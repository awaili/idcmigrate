"""FastAPI backend for idc-migrate.

REST + WebSocket. Reuses ``idc.core`` / ``idc.llm`` / ``idc.agent`` verbatim.
Serves the single-page web UI from ``web/`` at ``/``.

Run: ``python -m idc.backend.app`` or ``idc serve``.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Dict, List, Literal, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response as StarletteResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import ROOT, get_settings, load_dotenv
from ..core import open_store, rebuild, run_ingest
from ..core.db import ServerFilter
from ..core.ingest import all_sources
from ..core.models import (ALL_QUESTION_KINDS, ChangeJob, CodeProfile, DBConversionProfile,
                           Question, ScanFinding, _now)
from ..llm import get_client
from ..agent import ExecutorError, build_context, get_executor_client, run_agent
from ..core.codeintel import app_targets, build_change_spec, resolve_value
from ..core.match import warranty_bucket
from ..core.eol import os_eol_bucket

load_dotenv()
settings = get_settings()
STORE = open_store(settings.db_url)
LLM = get_client(settings)

# task_id -> asyncio.Queue of agent events (in-memory event bus)
_EVENT_BUS: Dict[str, "asyncio.Queue"] = {}

app = FastAPI(title="idc-migrate", version="0.1.0")

# ---------------------------------------------------------------------------
# Web auth — shared-password login gate over the UI + API.
# ---------------------------------------------------------------------------
# A single shared password (IDC_WEB_PASSWORD env, or a DB system_config
# `web_password` override set at runtime). When no password is configured, auth
# is OFF and the app is open (backwards-compatible + lets the test suite run).
# When a password is set, every non-public request needs EITHER a valid session
# cookie (browser login) OR a valid executor bearer token (so the external
# executor's callbacks + context pulls still work). Public: /login, /logout,
# /executor (the contract page), /assets/*.
_SESSION_SECRET = settings.web_session_secret or secrets.token_hex(32)
# Sentinel returned by _web_auth_password on DB error: non-empty so auth stays
# ON (fail closed — never silently open the app on a DB outage), but no form
# password ever equals it, so every login is rejected until the DB recovers.
_DB_ERR_PW = "\x00\x01db-error"


def _web_auth_password() -> str:
    """The active login password: DB override if set, else env. '' = auth off.
    On DB error returns a fail-closed sentinel (auth stays on, login denied)."""
    try:
        db = STORE.get_config("web_password")
    except Exception:
        return _DB_ERR_PW
    return db or settings.web_password


def _executor_bearer_ok(authorization: str) -> bool:
    tok = _executor_settings().executor_token
    if not tok or not authorization or not authorization.lower().startswith("bearer "):
        return False
    return secrets.compare_digest(authorization.split(" ", 1)[1].strip(), tok)


# crude in-memory login throttle: >10 failures per IP in 60s -> 429. Process-
# local (single backend); resets on restart. Guards the shared-password gate
# against brute force on the internet-facing box.
_LOGIN_FAILS: Dict[str, list] = {}
_LOGIN_FAIL_WINDOW = 60.0
_LOGIN_FAIL_MAX = 10


def _login_throttled(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_FAIL_WINDOW]
    _LOGIN_FAILS[ip] = recent
    return len(recent) >= _LOGIN_FAIL_MAX


def _record_login_fail(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(time.time())


def _ws_session_authed(ws: WebSocket) -> bool:
    """The HTTP auth middleware doesn't run for WebSocket scopes, so check the
    session cookie manually here (same signing as SessionMiddleware). Auth-off
    when no password is configured."""
    if not _web_auth_password():
        return True
    cookie = ws.cookies.get("idc_sess")
    if not cookie:
        return False
    try:
        from itsdangerous import URLSafeTimedSerializer, BadSignature
        signer = URLSafeTimedSerializer(_SESSION_SECRET, salt="cookie-session")
        data = signer.loads(cookie, max_age=60 * 60 * 24 * 30)
        return bool((data.get("session") or {}).get("authed"))
    except Exception:  # BadSignature / expired / malformed -> deny
        return False


# paths that stay open when auth is on
_PUBLIC_PREFIXES = ("/assets/",)
_PUBLIC_PATHS = {"/login", "/logout", "/executor", "/favicon.ico"}
# Paths where the executor bearer token is honored (the executor contract
# surface: push callbacks + read-only context pulls). Everywhere else under
# /api/ requires a browser session — so a leaked bearer can't change the
# password, rewrite executor config, trigger scans, or drive migration jobs.
_BEARER_PREFIXES = ("/api/code-profiles", "/api/db-profiles", "/api/change-jobs",
                    "/api/workloads/", "/api/apps/", "/api/questions")


async def _auth_dispatch(request: Request, call_next):
    pw = _web_auth_password()
    if not pw:  # auth disabled -> pass through
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    if request.session.get("authed"):
        return await call_next(request)
    if _executor_bearer_ok(request.headers.get("authorization", "")) \
       and any(path.startswith(p) for p in _BEARER_PREFIXES):
        return await call_next(request)
    # denied: JSON for API/WS, redirect for pages
    if path.startswith("/api/") or path.startswith("/ws/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# Order matters: Session must be OUTERMOST so request.session is populated
# before _auth_dispatch reads it. add_middleware prepends, so add Auth first,
# Session second (Session ends up outermost). https_only is left False so the
# cookie also works for direct localhost admin + the test transport; HTTPS is
# enforced at the nginx edge (port 80 -> 443 redirect) so the browser only
# ever sends it over TLS.
app.add_middleware(BaseHTTPMiddleware, dispatch=_auth_dispatch)
app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET,
                   session_cookie="idc_sess", same_site="lax", https_only=False,
                   max_age=60 * 60 * 24 * 30)

# ---------------------------------------------------------------------------
# Live executor config — operator-editable at runtime via /api/executor/config
# ---------------------------------------------------------------------------
# The executor connection is env-driven by default (IDC_EXECUTOR_URL/TOKEN/
# ENABLED/TIMEOUT), but the web "Manage executor" panel can override it without
# a redeploy. Overrides persist in the system_config table so they survive
# restart; on startup we layer DB values over the env defaults.
_EXEC_CFG_KEYS = ("executor_url", "executor_token", "executor_enabled", "executor_timeout")
_EXEC_TRUTHY = ("1", "true", "yes", "on")


def _executor_settings():
    """A Settings copy with the executor fields overlaid from DB system_config
    (operator-managed via /api/executor/config) on top of the CURRENT env
    Settings. Read live on every call — no stale cache — so test monkeypatches
    of ``settings.executor_*`` and runtime DB PUTs both take effect instantly.
    The DB round-trip is fine: executor calls are infrequent operator actions /
    push callbacks, not hot paths."""
    import dataclasses
    base = {k: getattr(settings, k) for k in _EXEC_CFG_KEYS}
    ov = STORE.get_config_map(_EXEC_CFG_KEYS)
    if "executor_url" in ov:
        base["executor_url"] = ov["executor_url"]
    if "executor_token" in ov:
        base["executor_token"] = ov["executor_token"]
    if "executor_enabled" in ov:
        base["executor_enabled"] = str(ov["executor_enabled"]).strip().lower() in _EXEC_TRUTHY
    if "executor_timeout" in ov:
        try:
            base["executor_timeout"] = int(ov["executor_timeout"])
        except (TypeError, ValueError):
            pass
    return dataclasses.replace(settings, **base)

# Landing dir for uploaded source files (CSV/xlsx/JSON). Persisted so a later
# non-upload ingest can re-read them if the operator points IDC_*_PATH here.
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Accepted upload extensions per source (light validation; the adapter is the
# final authority on content).
_UPLOAD_EXT = {
    "servicenow": {".csv"},
    "rvtools":    {".csv", ".xlsx", ".xls"},
    "zabbix":     {".json"},
    "prometheus": {".json"},
}


# ---------------------------------------------------------------------------
# request / response schemas
# ---------------------------------------------------------------------------
class IngestReq(BaseModel):
    source: str = "all"


class RebuildReq(BaseModel):
    do_match: bool = True
    do_plan: bool = True
    # hard cap on total waves (0 = no cap); unifies the guarantee with plan-llm
    max_waves: int = 0


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
def _server_out(s, profiles_by_app=None, strategies_by_app=None):
    d = s.to_dict()
    m = STORE.get_match(s.id)
    d["match"] = m.to_dict() if m else None
    # data-gap — precomputed support buckets so the inventory list + drawer can
    # render warranty / OS-EOL badges without duplicating the EOL table in JS.
    # Prefer the persisted bucket (B1 gap-actionable); fall back to computing
    # for stale rows that predate the column.
    d["warranty_bucket"] = s.warranty_bucket or warranty_bucket(s)
    d["os_eol_bucket"] = s.os_eol_bucket or os_eol_bucket(s)
    # code-scan enrichment: only when a profile map is supplied (single-server
    # detail). The paginated list endpoint does NOT pass one, so it stays fast
    # and does not load all profiles per row.
    if profiles_by_app is not None:
        d["code"] = _server_code_summary(s, profiles_by_app, strategies_by_app)
    return d


def _server_code_summary(s, profiles_by_app, strategies_by_app=None):
    """Per-app code-scan signals for the server's apps — the executor's scan
    feedback surfaced as resource detail (used as migration-strategy reference).

    Read-only rollup of each app's CodeProfile: runtime/framework, cloud
    readiness, the executor's migration_pattern + effort, blockers, code-discovered
    deps + network endpoints, and finding-category counts. When an AI 7R strategy
    exists for the app (``strategies_by_app``), it's attached as ``ai_strategy``
    alongside the executor's pattern — the two never overwrite each other. Empty
    when none of the server's apps have been scanned."""
    out = []
    for app_id in (s.app_ids or []):
        p = profiles_by_app.get(app_id)
        if p is None:
            continue
        cats: Dict[str, int] = {}
        for f in (p.findings or []):
            cats[f.category] = cats.get(f.category, 0) + 1
        entry = {
            "app_id": p.app_id, "language": p.language, "runtime": p.runtime,
            "framework": p.framework,
            "cloud_readiness": p.cloud_readiness,
            "migration_pattern": p.migration_pattern,
            "refactor_effort": p.refactor_effort,
            "blockers": list(p.blockers or []),
            "code_deps": list(p.code_deps or []),
            "network_endpoints": list(p.network_endpoints or []),
            "finding_categories": cats,
            "findings_count": len(p.findings or []),
            "required_changes_count": len(p.required_changes or []),
            "summary": p.summary, "scanned_at": p.scanned_at,
        }
        st = strategies_by_app.get(app_id) if strategies_by_app else None
        if st is not None:
            entry["ai_strategy"] = {
                "strategy": st.strategy, "rationale": st.rationale,
                "target": st.target, "confidence": st.confidence,
                "effort": st.effort, "key_changes": list(st.key_changes or []),
                "source": st.source, "assigned_at": st.assigned_at,
            }
        out.append(entry)
    return out


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
                 warranty_bucket: Optional[str] = None,
                 os_eol_bucket: Optional[str] = None,
                 page: int = 1, page_size: int = 50,
                 order_by: str = "hostname", order_dir: str = "asc",
                 facets: bool = True):
    """Paginated, filtered, faceted server query (scales to 15K+).

    ``facets=false`` skips the 6 facet GROUP-BY queries — callers that only
    need items+total (the wave-members view) use it to stay fast for big waves."""
    f = ServerFilter(role=role, env=env, status=status, source_type=source_type,
                     os=os, criticality=criticality, cluster=cluster, datacenter=datacenter,
                     target_product=target_product, wave_id=wave_id, q=q,
                     util_cpu_min=util_cpu_min, util_mem_min=util_mem_min,
                     util_disk_min=util_disk_min, conf_min=conf_min, conf_max=conf_max,
                     warranty_bucket=warranty_bucket, os_eol_bucket=os_eol_bucket)
    res = STORE.query_servers(f, page=page, page_size=min(page_size, 500),
                              order_by=order_by, order_dir=order_dir,
                              with_facets=facets)
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
    # attach code-scan enrichment (one profile + strategy load, not per-row)
    pmap = {p.app_id: p for p in STORE.list_code_profiles()}
    smap = {s2.app_id: s2 for s2 in STORE.list_app_strategies()}
    return _server_out(s, profiles_by_app=pmap, strategies_by_app=smap)


@app.put("/api/servers/{sid}/warranty")
def put_server_warranty(sid: str, body: dict):
    """Operator override of a server's hardware support status (data-gap).

    Traditional-IDC CMDBs often lack warranty / end-of-support data; the
    operator can set it per host here so the F2 on-prem extended-support
    premium, the F10 ``hw_support`` readiness signal, and the data-gaps
    "unknown warranty" count reflect reality. Both fields optional — send
    either/both; empty string clears. Persists immediately (no rebuild needed).
    """
    s = STORE.get_server(sid)
    if not s:
        raise HTTPException(404, "server not found")
    ws = (body.get("warranty_status") or "").strip().lower()
    if ws and ws not in ("active", "expiring", "expired", "unknown"):
        raise HTTPException(400, "warranty_status must be active/expiring/expired/unknown")
    if "warranty_status" in body:
        s.warranty_status = ws
    if "hardware_eol" in body:
        s.hardware_eol = (body.get("hardware_eol") or "").strip()
    s.warranty_bucket = warranty_bucket(s)   # recompute the derived bucket
    STORE.upsert_server(s)
    return {"server_id": sid, "warranty_status": s.warranty_status,
            "hardware_eol": s.hardware_eol, "warranty_bucket": s.warranty_bucket,
            "updated": True}


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
    # hard cap on total instantiated waves; the LLM is re-prompted to revise
    # if the engine produces more. 0/None = no cap (also auto-parsed from demand).
    max_waves: Optional[int] = None


class PlanApplyReq(BaseModel):
    waves: List[dict]


@app.post("/api/plan/propose")
def plan_propose(req: PlanProposeReq):
    """Ask the MigraQ for a wave policy from the demand, instantiate it (dry-run).
    Does NOT persist — call /api/plan/apply to save."""
    from ..core.llm_plan import validate_plan
    from ..llm import get_planner
    servers = STORE.list_all_servers()
    wls = STORE.list_workloads()
    matches = STORE.list_matches()
    profiles = STORE.list_code_profiles()
    strategies = {s.app_id: s for s in STORE.list_app_strategies()}
    scope_ids = None
    if req.scope == "filter":
        f = ServerFilter(role=req.role, env=req.env, os=req.os,
                         source_type=req.source_type, criticality=req.criticality,
                         cluster=req.cluster, target_product=req.target_product,
                         q=req.q, util_mem_min=req.util_mem_min, conf_max=req.conf_max)
        scope_ids = set(STORE.query_server_ids(f))
    res = get_planner(settings).propose(req.demand, servers, wls, matches, scope_ids,
                                        max_waves=req.max_waves, profiles=profiles,
                                        strategies=strategies)
    pol = res.get("policy")
    if not pol:
        return {"ok": False, "errors": res.get("errors"), "raw": res.get("raw"),
                "policy": None, "waves": [], "validation": None,
                "max_waves": res.get("max_waves"), "revisions": res.get("revisions", [])}
    # propose returns the authoritative, already-capped + 7R-excluded waves —
    # use them directly (no re-instantiation: cap + retain/retire can't diverge).
    waves = res.get("waves") or []
    by_id = {s.id: s for s in servers}
    val = validate_plan(waves, servers)
    total = len(scope_ids) if scope_ids is not None else len(servers)
    assigned = len({sid for w in waves for sid in w.server_ids})
    return {
        "ok": not res.get("errors"),
        "policy": {"notes": pol.notes,
                   "stages": [{k: v for k, v in vars(s).items() if v not in ([], "", None, True) or k in ("label", "stage", "group_by", "use_app_deps")}
                              for s in pol.stages]},
        "waves": [w.to_dict() for w in waves],
        "wave_hostnames": [{**w.to_dict(), "members": [by_id[sid].hostname for sid in w.server_ids if sid in by_id]}
                           for w in waves[:200]],
        "stats": {"total": total, "assigned": assigned,
                  "waves": len(waves), "unassigned": total - assigned},
        "max_waves": res.get("max_waves"),
        "revisions": res.get("revisions", []),
        "engine_capped": res.get("engine_capped"),
        "validation": val,
        "errors": res.get("errors") or [],
        "warnings": res.get("warnings") or [],
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


@app.post("/api/ingest/upload")
async def ingest_upload(source: str = Form(...), file: UploadFile = File(...)):
    """Upload a source file (CSV/xlsx/JSON) and ingest just that source.

    The file is saved under ``uploads/`` and ingested in offline/file mode for
    this run only (live API creds are ignored). Accepts one source at a time:
    servicenow (.csv), rvtools (.csv/.xlsx), zabbix (.json), prometheus (.json).
    """
    if source not in all_sources():
        raise HTTPException(400, f"unknown source {source!r}; "
                                  f"expected one of {all_sources()}")
    ext = os.path.splitext(file.filename or "")[1].lower()
    allowed = _UPLOAD_EXT.get(source, set())
    if ext not in allowed:
        raise HTTPException(400, f"{source} expects {sorted(allowed)}; got {ext or '(none)'}")

    # stable filename: <source>__<original>; collisions overwrite (re-ingest)
    safe = (file.filename or f"{source}{ext}").replace(os.sep, "_").lstrip("/ ")
    dest = UPLOAD_DIR / f"{source}__{safe}"
    with open(dest, "wb") as fh:
        while True:
            chunk = await file.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            fh.write(chunk)

    runs = run_ingest(STORE, settings, source, path_overrides={source: str(dest)})
    if runs and runs[0].error:
        raise HTTPException(422, f"ingest of {source} failed: {runs[0].error}")
    return {"source": source, "path": str(dest), "run": runs[0].to_dict() if runs else None}


# ---------------------------------------------------------------------------
# REST: external agent executor (code scan / comb / modify)
#   push direction (executor → idc-migrate) is bearer-authed.
# ---------------------------------------------------------------------------
def _check_executor_auth(authorization: Optional[str]):
    """Validate the bearer token against the configured executor token (live
    config, so a token change via /api/executor/config takes effect immediately)."""
    token = _executor_settings().executor_token
    if not token:
        raise HTTPException(401, "IDC_EXECUTOR_TOKEN not configured; "
                                  "set it to accept executor push callbacks")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    if not secrets.compare_digest(authorization.split(" ", 1)[1].strip(), token):
        raise HTTPException(401, "invalid executor token")


@app.get("/api/code-profiles")
def list_code_profiles():
    return [p.to_dict() for p in STORE.list_code_profiles()]


@app.get("/api/code-profiles/{app_id}")
def get_code_profile(app_id: str):
    p = STORE.get_code_profile(app_id)
    if not p:
        raise HTTPException(404, "no code profile for app")
    return p.to_dict()


# -- executor ongoing-interaction (read-only context pulls + resolve) --------
# These let the executor ASK idc-migrate while it works, instead of working
# off a frozen upfront snapshot. See docs/agent-executor.md §2.5.
@app.get("/api/workloads/{app_id}")
def get_workload(app_id: str):
    """App graph context for the executor: tier, depends_on, servers.

    Read-only — no bearer auth (same as the other GET /api/code-profiles and
    GET /api/change-jobs read paths). The executor uses this to know an app's
    blast radius / dependency edges before modifying it.
    """
    w = next((x for x in STORE.list_workloads() if x.app_id == app_id), None)
    if w is None:
        raise HTTPException(404, "no workload for app")
    return w.to_dict()


@app.get("/api/apps/{app_id}/targets")
def get_app_targets(app_id: str):
    """Matched migration targets for an app's servers (CDB? CVM? which region?).

    The executor uses this to parameterize code toward the right cloud form —
    e.g. a DB server matched to ``CDB`` means the JDBC string should become a
    CDB-style host, not a bare-IP connection.
    """
    wls = STORE.list_workloads()
    w = next((x for x in wls if x.app_id == app_id), None)
    if w is None:
        raise HTTPException(404, "no workload for app")
    targets = app_targets(wls, STORE.list_matches(), app_id)
    return {"app_id": app_id, "targets": [
        {"server_id": m.server_id,
         "product": m.target.product, "spec": m.target.spec,
         "region": m.target.region, "az": m.target.az,
         "confidence": m.confidence, "rationale": m.rationale}
        for m in targets]}


@app.get("/api/apps/{app_id}/resolve")
def resolve_app_value(app_id: str, old: str = "", kind: str = "ip"):
    """Answer "what should this old value become?" for the executor.

    Called when the executor finds something mid-scan that wasn't in the
    upfront ``changes`` list (e.g. a second hardcoded IP). Resolution order:
    operator override → match-derived placeholder form → generic placeholder;
    if none applies, returns ``new=null, source="unknown"`` so the executor
    knows it must ask the operator (it must NOT guess).
    """
    if not old:
        raise HTTPException(400, "old query param required")
    # operator overrides live on the modify request; there is no per-app
    # override store yet, so resolve from the profile's network_endpoints +
    # the app's matched targets only. The endpoint shape is stable so adding
    # an override store later doesn't break the executor.
    matches = app_targets(STORE.list_workloads(), STORE.list_matches(), app_id)
    res = resolve_value(kind, old, matches=matches, overrides=None)
    res["app_id"] = app_id
    return res


# -- executor ↔ operator: questions the executor raises mid-job --------------
# The executor raises a question when it can't resolve a change from the
# profile/overrides (resolve=unknown, or a finding's old/new was empty). The
# operator answers via the UI; the executor polls GET /api/questions/{id}.
# This is the last "经常交互" channel — it turns "I don't know" into a
# blocking question instead of a silent guess or a stall.
@app.post("/api/apps/{app_id}/questions")
def raise_question(app_id: str, body: dict, authorization: Optional[str] = Header(None)):
    """Executor raises a question to the operator (bearer auth).

    Body: ``{job_id?, kind, prompt, options?, context?}`` where kind is
    ``value|choice|confirm``. Returns ``{id, status:"pending"}`` — the executor
    then polls ``GET /api/questions/{id}`` until ``status=answered``.
    """
    _check_executor_auth(authorization)
    kind = body.get("kind", "value")
    if kind not in ALL_QUESTION_KINDS:
        raise HTTPException(400, f"kind must be one of {ALL_QUESTION_KINDS}")
    q = Question(app_id=app_id, job_id=body.get("job_id", ""), kind=kind,
                 prompt=body.get("prompt", "") or "",
                 options=body.get("options") or [],
                 context=body.get("context") or {})
    if not q.prompt:
        raise HTTPException(400, "prompt required")
    STORE.upsert_question(q)
    return {"id": q.id, "status": q.status}


@app.get("/api/questions/{qid}")
def get_question(qid: str):
    """Read one question — executor polls this until answered; UI reads to show."""
    q = STORE.get_question(qid)
    if q is None:
        raise HTTPException(404, "no such question")
    return q.to_dict()


@app.get("/api/apps/{app_id}/questions")
def list_app_questions(app_id: str, status: Optional[str] = None):
    """List questions for an app (UI surfaces pending ones to the operator)."""
    return [q.to_dict() for q in STORE.list_questions(app_id=app_id, status=status)]


@app.get("/api/questions")
def list_questions(status: Optional[str] = None):
    """List all questions across apps (UI "pending queue" panel).

    ``status`` optional filter (pending / answered / skipped); omit for all.
    Used by the Code-tab questions panel to show the operator everything the
    executor is currently blocked on, regardless of app.
    """
    return [q.to_dict() for q in STORE.list_questions(status=status)]


@app.post("/api/questions/{qid}/answer")
def answer_question(qid: str, body: dict):
    """Operator answers a pending question (no bearer — it's the operator via UI).

    Body: ``{answer, answered_by?}``. Sets ``status=answered``; the executor's
    next poll of ``GET /api/questions/{id}`` sees the answer and continues.
    """
    q = STORE.get_question(qid)
    if q is None:
        raise HTTPException(404, "no such question")
    if q.status != "pending":
        raise HTTPException(409, f"question already {q.status}")
    ans = (body.get("answer") or "").strip()
    if not ans:
        raise HTTPException(400, "answer required")
    q.status = "answered"
    q.answer = ans
    q.answered_by = body.get("answered_by", "")
    q.answered_at = _now()
    STORE.upsert_question(q)
    return {"id": qid, "status": q.status, "answer": q.answer}


@app.post("/api/questions/{qid}/skip")
def skip_question(qid: str, body: Optional[dict] = None):
    """Operator dismisses a question (executor then skips that change)."""
    q = STORE.get_question(qid)
    if q is None:
        raise HTTPException(404, "no such question")
    if q.status != "pending":
        raise HTTPException(409, f"question already {q.status}")
    q.status = "skipped"
    q.answer = (body or {}).get("reason", "")
    q.answered_at = _now()
    STORE.upsert_question(q)
    return {"id": qid, "status": q.status}


@app.put("/api/code-profiles/{app_id}")
def put_code_profile(app_id: str, body: dict, authorization: Optional[str] = Header(None)):
    """Executor pushes/overwrites a CodeProfile for an app (bearer auth)."""
    _check_executor_auth(authorization)
    if body.get("app_id") and body["app_id"] != app_id:
        raise HTTPException(400, "app_id in body must match path")
    body["app_id"] = app_id
    try:
        p = CodeProfile.from_dict(body)
        findings = [ScanFinding.from_dict(x) for x in (body.get("findings") or [])]
        p.findings = findings
    except Exception as e:
        raise HTTPException(400, f"invalid CodeProfile: {e!r}")
    STORE.upsert_code_profile(p)
    return {"app_id": app_id, "updated": True}


@app.delete("/api/code-profiles/{app_id}")
def delete_code_profile(app_id: str, authorization: Optional[str] = Header(None)):
    _check_executor_auth(authorization)
    STORE.delete_code_profile(app_id)
    return {"app_id": app_id, "deleted": True}


# ---------------------------------------------------------------------------
# F5 — DB heterogeneous conversion profiles (executor pushes; UI / agent reads)
# ---------------------------------------------------------------------------
@app.get("/api/db-profiles")
def list_db_profiles():
    """List all DB conversion profiles (read-only; keyed by DB host identity)."""
    return [d.to_dict() for d in STORE.list_db_profiles()]


@app.get("/api/db-profiles/{db_server_id}")
def get_db_profile(db_server_id: str):
    p = STORE.get_db_profile(db_server_id)
    if not p:
        raise HTTPException(404, f"no DB profile for {db_server_id}")
    return p.to_dict()


@app.put("/api/db-profiles/{db_server_id}")
def put_db_profile(db_server_id: str, body: dict,
                   authorization: Optional[str] = Header(None)):
    """Executor pushes/overwrites a DBConversionProfile for a DB host (F5).

    ``db_server_id`` is the DB host's STABLE identity (hostname), not the
    transient Server.id — so the profile survives rebuilds (server ids are
    fresh each rebuild). Bearer auth, same as code-profiles."""
    _check_executor_auth(authorization)
    if body.get("db_server_id") and body["db_server_id"] != db_server_id:
        raise HTTPException(400, "db_server_id in body must match path")
    body["db_server_id"] = db_server_id
    try:
        d = DBConversionProfile.from_dict(body)
    except Exception as e:
        raise HTTPException(400, f"invalid DBConversionProfile: {e!r}")
    STORE.upsert_db_profile(d)
    return {"db_server_id": db_server_id, "updated": True}


@app.delete("/api/db-profiles/{db_server_id}")
def delete_db_profile(db_server_id: str, authorization: Optional[str] = Header(None)):
    _check_executor_auth(authorization)
    STORE.delete_db_profile(db_server_id)
    return {"db_server_id": db_server_id, "deleted": True}


@app.get("/api/change-jobs")
def list_change_jobs():
    return STORE.list_change_jobs()


@app.post("/api/change-jobs")
def post_change_job(body: dict, authorization: Optional[str] = Header(None)):
    """Executor reports/heartbeats a ChangeJob (bearer auth)."""
    _check_executor_auth(authorization)
    try:
        j = ChangeJob.from_dict(body)
        if not j.id:
            raise ValueError("id required")
    except Exception as e:
        raise HTTPException(400, f"invalid ChangeJob: {e!r}")
    STORE.upsert_change_job(j)
    return {"id": j.id, "status": j.status}


# -- request direction (idc-migrate → executor) ---------------------------
class ExecutorScanReq(BaseModel):
    app_id: str
    repo_url: str
    branch: str = ""
    action: Literal["scan", "comb", "modify"] = "scan"
    mode: Literal["plan", "execute"] = "plan"   # for modify
    scope: Optional[List[str]] = None
    # for modify: operator-supplied old→new value map (e.g.
    # {"10.0.4.20": "${DB_HOST}", "hunter2": "${DB_PASSWORD}"}). idc-migrate
    # folds these into the concrete change list it builds from the app's
    # CodeProfile, so the executor is told exactly what to replace.
    overrides: Optional[Dict[str, str]] = None


@app.get("/api/executor/status")
def executor_status_endpoint():
    """Connectivity probe for the external code-modifying executor — powers the
    web status indicator + the `doctor` CLI check. Never raises."""
    from ..agent import executor_status
    return executor_status(_executor_settings())


class ExecutorConfigReq(BaseModel):
    # All optional — only provided fields are updated on PUT; /test probes a
    # candidate {url, token} without persisting.
    url: Optional[str] = None
    token: Optional[str] = None
    enabled: Optional[bool] = None
    timeout: Optional[int] = None


def _executor_config_out(s=None) -> dict:
    """Public view of the executor config (token NEVER returned — only token_set)
    plus a live status probe."""
    from ..agent import executor_status
    s = s or _executor_settings()
    status = executor_status(s)
    return {
        "url": s.executor_url or "",
        "token_set": bool(s.executor_token),
        "enabled": bool(s.executor_enabled),
        "timeout": int(s.executor_timeout or 0),
        "status": status,
    }


@app.get("/api/executor/config")
def executor_get_config():
    """Current executor connection (URL / enabled / timeout / token_set) + a
    live health probe. The token itself is never returned."""
    return _executor_config_out()


@app.put("/api/executor/config")
def executor_put_config(req: ExecutorConfigReq):
    """Update the executor connection at runtime. Only provided fields change;
    the token is kept as-is when not supplied (so the panel can edit the URL
    without re-entering the secret). Persists to system_config so it survives a
    restart; _executor_settings() reads the DB live, so it takes effect
    immediately. Returns the new config + a probe."""
    if req.url is not None:
        if req.url and not (req.url.startswith("http://") or req.url.startswith("https://")):
            raise HTTPException(400, "url must be http(s)://...")
        STORE.set_config("executor_url", req.url)
    if req.token is not None and req.token != "":
        # empty token in the body means "leave unchanged" (the field was masked)
        STORE.set_config("executor_token", req.token)
    if req.enabled is not None:
        STORE.set_config("executor_enabled", "true" if req.enabled else "false")
    if req.timeout is not None:
        if not (1 <= req.timeout <= 3600):
            raise HTTPException(400, "timeout must be between 1 and 3600 seconds")
        STORE.set_config("executor_timeout", str(int(req.timeout)))
    return _executor_config_out()


@app.post("/api/executor/test")
def executor_test_config(req: ExecutorConfigReq):
    """Probe a candidate executor connection WITHOUT persisting — the panel's
    "Test connection" button. Layer the provided url/token over the live config
    (falling back to live values for fields not supplied) and run the probe."""
    import dataclasses
    s = _executor_settings()
    overrides: Dict[str, Any] = {}
    if req.url is not None:
        overrides["executor_url"] = req.url
    if req.token is not None and req.token != "":
        overrides["executor_token"] = req.token
    if overrides:
        s = dataclasses.replace(s, **overrides)
    from ..agent import executor_status
    return executor_status(s)


@app.post("/api/executor/trigger")
def executor_trigger(req: ExecutorScanReq):
    """Ask the external executor to scan/comb/modify an app's repo (async).

    Returns the executor's ``{job_id, status}``. The executor calls back into
    ``PUT /api/code-profiles/{app_id}`` / ``POST /api/change-jobs`` when done.

    For ``modify``: idc-migrate builds a concrete change list from the app's
    stored ``CodeProfile`` (joined with the operator ``overrides``) and sends
    it, so the executor knows *which file/line, which literal, which value* —
    it does not re-scan or guess. If no profile exists yet the change list is
    empty and the spec's ``notes`` tell the executor to scan first.
    """
    if not _executor_settings().executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(_executor_settings())
    if not ec.configured:
        raise HTTPException(409, "IDC_EXECUTOR_URL not configured")
    # The callback base is configured on the executor side (it pushes back to
    # this server's /api/code-profiles + /api/change-jobs endpoints).
    try:
        if req.action == "modify":
            profile = STORE.get_code_profile(req.app_id)
            # F9 — runtime-derived profiles (no source) must be operator-confirmed
            # before the executor writes the inferred scaffold. In plan mode the
            # scaffold is previewed freely; in execute mode the confirm-gate fires
            # (a Question is raised; the operator answers, then re-triggers).
            from ..core.codeintel import is_runtime_derived, runtime_confirm_question
            if is_runtime_derived(profile) and req.mode == "execute":
                q = runtime_confirm_question(req.app_id, profile)
                if q is not None:
                    STORE.upsert_question(q)
                    raise HTTPException(409, f"runtime-derived scaffold requires "
                                               f"operator confirmation (question {q.id}); "
                                               f"answer it then re-trigger modify")
            spec = build_change_spec(profile, scope=req.scope,
                                     overrides=req.overrides)
            res = ec.modify(req.app_id, req.repo_url, req.branch,
                             mode=req.mode, scope=req.scope,
                             changes=spec.changes, notes=spec.notes)
            # surface the concrete change list + notes back to the operator so
            # they can see exactly what was sent to the executor (audit/preview).
            if isinstance(res, dict):
                res.setdefault("changes", spec.changes)
                res.setdefault("notes", spec.notes)
            return res
        if req.action == "comb":
            return ec.comb(req.app_id, req.repo_url, req.branch)
        return ec.scan(req.app_id, req.repo_url, req.branch)
    except ExecutorError as e:
        raise HTTPException(502, f"executor error: {e}")


class DBScanReq(BaseModel):
    db_server_id: str          # stable DB host identity (hostname)
    source_engine: str = ""    # oracle / sqlserver / mysql
    target_engine: str = ""   # tdsql / cdb_mysql


@app.post("/api/db-scan")
def db_scan_trigger(req: DBScanReq):
    """Ask the external executor to run a heterogeneous DB-scan on a DB host (F5).

    The executor scans the DB schema/SQL and pushes a ``DBConversionProfile``
    back to ``PUT /api/db-profiles/{db_server_id}``. Rebuild then folds the
    grade into the host's match (confidence dip / replatform force) + wave risk.
    """
    if not _executor_settings().executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(_executor_settings())
    if not ec.configured:
        raise HTTPException(409, "IDC_EXECUTOR_URL not configured")
    try:
        return ec.db_scan(req.db_server_id, req.source_engine, req.target_engine)
    except ExecutorError as e:
        raise HTTPException(502, f"executor error: {e}")


class RuntimeContainerizeReq(BaseModel):
    app_id: str
    server_id: str                 # host whose runtime inventory we infer from
    inventory: Dict[str, Any] = {}  # process / port / software (Zabbix/Prometheus)
    mode: Literal["plan", "execute"] = "plan"   # plan (dry-run) | execute (write)


@app.get("/api/runtime-inventory/{server_id}")
def runtime_inventory(server_id: str):
    """Best-effort runtime inventory for a host (F9) — assembles process/port/
    software from the match port + server role/os + (stale) code profile +
    Zabbix/Prometheus listening-port telemetry when configured. The web form
    pre-fills from this; the operator overrides before triggering."""
    from ..core.runtime_inventory import gather_runtime_inventory
    s = next((x for x in STORE.list_all_servers() if x.id == server_id), None)
    if not s:
        raise HTTPException(404, f"no such server: {server_id}")
    matches = STORE.list_matches()
    m = next((x for x in matches if x.server_id == server_id), None)
    profile = STORE.get_code_profile((s.app_ids or [""])[0]) if s.app_ids else None
    return gather_runtime_inventory(s, m, profile=profile, settings=settings)


@app.post("/api/runtime-containerize")
def runtime_containerize_trigger(req: RuntimeContainerizeReq):
    """Ask the external executor to containerize a source-less legacy app (F9).

    No ``repo_url`` — the executor infers a Dockerfile scaffold from the host's
    runtime inventory (process/port/software from Zabbix/Prometheus) and pushes
    a ``CodeProfile`` tagged ``source=runtime-derived`` back to
    ``PUT /api/code-profiles/{app_id}``. That profile's confidence is capped and
    a confirm-gate fires before ``modify`` writes the scaffold.

    When ``inventory`` is empty/partial, idc-migrate auto-gathers it from the
    server's match port + role/os + telemetry (see ``GET /api/runtime-inventory``)
    and merges the operator-provided fields on top (operator wins)."""
    if not _executor_settings().executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(_executor_settings())
    if not ec.configured:
        raise HTTPException(409, "IDC_EXECUTOR_URL not configured")
    # auto-gather + merge when the operator didn't supply a full inventory
    from ..core.runtime_inventory import gather_runtime_inventory, merge_inventory
    inventory = dict(req.inventory or {})
    # always normalize operator input (software string -> list) so the executor
    # gets a consistent shape regardless of how the form sent it
    sw = inventory.get("software")
    if isinstance(sw, str):
        inventory["software"] = [s.strip() for s in sw.split(",") if s.strip()]
    # only hit telemetry (Zabbix/Prometheus round-trips) when the operator didn't
    # supply ports — the main runtime signal. Other gaps are filled from match/role.
    if not inventory.get("ports"):
        s = next((x for x in STORE.list_all_servers() if x.id == req.server_id), None)
        if s:
            matches = STORE.list_matches()
            m = next((x for x in matches if x.server_id == req.server_id), None)
            profile = STORE.get_code_profile((s.app_ids or [""])[0]) if s.app_ids else None
            gathered = gather_runtime_inventory(s, m, profile=profile, settings=settings)
            inventory = merge_inventory(gathered, inventory)
    try:
        return ec.runtime_containerize(req.app_id, req.server_id,
                                        inventory=inventory, mode=req.mode)
    except ExecutorError as e:
        raise HTTPException(502, f"executor error: {e}")


@app.get("/api/executor/jobs/{job_id}")
def executor_job(job_id: str):
    """Proxy a job-status poll to the executor."""
    ec = get_executor_client(_executor_settings())
    if not ec.configured:
        raise HTTPException(409, "IDC_EXECUTOR_URL not configured")
    try:
        return ec.get_job(job_id)
    except ExecutorError as e:
        raise HTTPException(502, f"executor error: {e}")


@app.post("/api/rebuild")
def rebuild_derived(req: RebuildReq):
    """Rebuild derived tables (servers/matches/waves) as an NDJSON stream.

    Streams ``{type:"progress", phase, ...}`` as each stage finishes (normalize
    → persist servers/workloads → match N → persist matches+costs → plan →
    persist waves), then ``{type:"done", ...stats}`` (or ``{type:"error"}``).
    The browser reads the stream for a live stage indicator and can cancel the
    watch via AbortController — the server-side rebuild still runs to completion.

    Why stream instead of a plain JSON response: rebuild is a full-estate write.
    For a 15K estate it previously took ~2 min with no bytes on the wire, so
    proxies/the browser could time out and the UI had no progress. The write
    path is now batched (one tx per table, see Store.replace_*), so it is
    seconds — but streaming keeps the connection alive for any estate size and
    gives the user a live stage indicator + cancel."""
    import queue as _queue
    import threading as _threading

    out_q: "_queue.Queue" = _queue.Queue()
    _SENTINEL = object()

    def _emit(frame: dict) -> None:
        out_q.put(json.dumps(frame, ensure_ascii=False) + "\n")

    def _on_progress(phase: str, payload: dict) -> None:
        frame = {"type": "progress", "phase": phase}
        frame.update(payload)
        _emit(frame)

    def _worker() -> None:
        try:
            stats = rebuild(STORE, settings, do_match=req.do_match,
                            do_plan=req.do_plan, max_waves=req.max_waves,
                            on_progress=_on_progress)
            _emit({"type": "done", **stats})
        except Exception as e:   # never leave the stream hanging
            _emit({"type": "error", "error": f"{e!r}"})
        finally:
            out_q.put(_SENTINEL)

    _threading.Thread(target=_worker, daemon=True).start()

    def stream():
        while True:
            item = out_q.get()
            if item is _SENTINEL:
                break
            yield item

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# REST: LLM
# ---------------------------------------------------------------------------
@app.post("/api/ask")
async def ask(req: AskReq):
    """Grounded estate Q&A: classify the question -> run a deterministic estate
    query -> narrate. The graph/aggregate answer is computed by the engine
    (no hallucination); the LLM only classifies + narrates. Falls back to the
    old keyword ``ask`` if the grounded path fails."""
    servers = STORE.list_all_servers()
    waves = STORE.list_waves()
    wls = STORE.list_workloads()
    matches = STORE.list_matches()
    profiles = STORE.list_code_profiles()
    strategies = {s.app_id: s for s in STORE.list_app_strategies()}
    res = LLM.ask_grounded(req.question, servers, waves, wls, matches, profiles, strategies)
    if res.get("ok"):
        return {"question": req.question, "answer": res["answer"],
                "intent": res["intent"], "params": res["params"],
                "result": res["result"], "grounded": True}
    # fallback to the keyword RAG
    answer = LLM.ask(req.question, servers, matches, k=req.k)
    return {"question": req.question, "answer": answer, "grounded": False,
            "error": res.get("answer")}


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


@app.post("/api/wave/assess")
def wave_assess(req: ExplainReq):
    """Risk assessment + runbook for one wave.

    ``req.server_id`` is overloaded to carry the wave_id (the endpoint is
    generic). Returns {ok, risk_score, risk_level, risk_factors, signals,
    go_no_go, runbook{pre_checks,cutover,rollback}, summary}. The risk score /
    level / factors are deterministic (reproducible); the runbook + go/no-go +
    summary are MigraQ-generated. On MigraQ failure the deterministic risk is still
    returned."""
    from ..core.models import Wave
    w = next((x for x in STORE.list_waves() if x.id == req.server_id), None)
    if w is None:
        raise HTTPException(404, "wave not found")
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    profiles = STORE.list_code_profiles()
    res = LLM.assess_wave(w, servers, matches, profiles)
    res["wave_id"] = w.id
    res["wave_name"] = w.name
    return res


@app.post("/api/match/audit")
def match_audit(req: ExplainReq):
    """AI second-opinion on the rule-based migration target for a server.

    Returns {ok, verdict (keep/change/review), confidence, critique,
    alternative_target, alternative_spec, rationale, risks}. Advisory only —
    does NOT change the stored Match. Uses the first app's CodeProfile if any."""
    s = STORE.get_server(req.server_id)
    if not s:
        raise HTTPException(404, "server not found")
    m = STORE.get_match(req.server_id)
    if not m:
        raise HTTPException(404, "no match for server")
    prof = None
    if s.app_ids:
        pmap = {p.app_id: p for p in STORE.list_code_profiles()}
        prof = next((pmap[a] for a in s.app_ids if a in pmap), None)
    res = LLM.audit_match(s, m, prof)
    res["server_id"] = req.server_id
    res["rule_target"] = {"product": m.target.product, "spec": m.target.spec,
                          "confidence": m.confidence}
    return res


@app.post("/api/plan/review")
def plan_review():
    """Red-team the current persisted wave plan for semantic issues the
    structural ``validate_plan`` can't catch (env mixing, criticality
    placement, blast radius, dependency-order, retain/retire leak, coverage).
    Returns {ok, overall (sound/needs-work/risky), findings, summary}. Advisory."""
    waves = STORE.list_waves()
    if not waves:
        raise HTTPException(404, "no waves to review — build a plan first")
    servers = STORE.list_all_servers()
    wls = STORE.list_workloads()
    matches = STORE.list_matches()
    profiles = STORE.list_code_profiles()
    strategies = {s.app_id: s for s in STORE.list_app_strategies()}
    res = LLM.review_plan(waves, servers, wls, matches, profiles, strategies)
    res["wave_count"] = len(waves)
    return res


# ---------------------------------------------------------------------------
# REST: 7R strategy (after reconsolidate)
# ---------------------------------------------------------------------------
class StrategyReq(BaseModel):
    app_id: str
    apply: bool = False     # persist the chosen strategy into app_strategies


@app.post("/api/strategy")
def assign_strategy(req: StrategyReq):
    """Assign a 7R migration strategy (6R + rehost-container) to one app via the
    MigraQ, given its reconsolidated context. Optionally persist into the
    ``app_strategies`` table (separate from CodeProfile — the executor's scan
    pattern is NOT overwritten). The wave planners read app_strategies as a 7R
    overlay, so retain/retire then drive wave exclusion on the next Rebuild /
    plan-llm."""
    from ..core.models import AppStrategy, STRATEGY_SOURCE_AI, _now
    servers = STORE.list_all_servers()
    wls = STORE.list_workloads()
    matches = STORE.list_matches()
    profiles = STORE.list_code_profiles()
    if req.app_id not in {w.app_id for w in wls}:
        raise HTTPException(404, f"app {req.app_id} not in the workload graph")
    res = LLM.seven_r_strategy(req.app_id, servers, wls, matches, profiles)
    if req.apply and res.get("ok"):
        STORE.upsert_app_strategy(AppStrategy(
            app_id=req.app_id, strategy=res["strategy"], rationale=res["rationale"],
            target=res["target"], confidence=res["confidence"], effort=res["effort"],
            key_changes=res["key_changes"], source=STRATEGY_SOURCE_AI,
            assigned_at=_now(), updated_at=_now()))
        res["applied"] = True
    return res


@app.get("/api/strategies")
def list_strategies():
    """List all AI-assigned 7R strategies (app_strategies table)."""
    return [s.to_dict() for s in STORE.list_app_strategies()]


@app.get("/api/strategies/{app_id}")
def get_strategy(app_id: str):
    s = STORE.get_app_strategy(app_id)
    if not s:
        raise HTTPException(404, "no strategy for app")
    return s.to_dict()


class StrategyBatchReq(BaseModel):
    apply: bool = False
    # app_ids to assign; default = all apps with a CodeProfile (where 7R is most
    # useful). Pass a subset to limit.
    app_ids: Optional[List[str]] = None


@app.post("/api/strategy/batch")
def assign_strategy_batch(req: StrategyBatchReq):
    """Batch-assign 7R strategies as an NDJSON stream (one object per app).

    Streams ``{type:"start", total}`` then one ``{type:"result", app_id, ok,
    strategy, ...}`` per app as the LLM finishes it (sequential — one gateway
    call at a time, no firehose), then ``{type:"done", total, ok_count}``.
    Browser reads the stream for live progress + can cancel via AbortController.
    Replaces the old client-side loop that did N sequential calls from the
    browser (which hung for large N)."""
    from ..core.models import AppStrategy, STRATEGY_SOURCE_AI, _now

    def stream():
        servers = STORE.list_all_servers()
        wls = STORE.list_workloads()
        matches = STORE.list_matches()
        profiles = STORE.list_code_profiles()
        app_ids = req.app_ids if req.app_ids is not None else [p.app_id for p in profiles]
        yield json.dumps({"type": "start", "total": len(app_ids),
                          "apply": req.apply}, ensure_ascii=False) + "\n"
        ok_count = 0
        for i, aid in enumerate(app_ids, 1):
            try:
                r = LLM.seven_r_strategy(aid, servers, wls, matches, profiles)
            except Exception as e:   # never let one app kill the stream
                r = {"ok": False, "app_id": aid, "error": f"{e!r}", "strategy": ""}
            if req.apply and r.get("ok"):
                try:
                    STORE.upsert_app_strategy(AppStrategy(
                        app_id=aid, strategy=r["strategy"], rationale=r["rationale"],
                        target=r["target"], confidence=r["confidence"], effort=r["effort"],
                        key_changes=r["key_changes"], source=STRATEGY_SOURCE_AI,
                        assigned_at=_now(), updated_at=_now()))
                    r["applied"] = True
                except Exception as e:
                    r["apply_error"] = f"{e!r}"
            if r.get("ok"):
                ok_count += 1
            r["type"] = "result"
            r["done"] = i
            r["total"] = len(app_ids)
            yield json.dumps(r, ensure_ascii=False) + "\n"
        yield json.dumps({"type": "done", "total": len(app_ids),
                          "ok_count": ok_count}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# F2 — TCO / business case + cost what-if
# ---------------------------------------------------------------------------
class BusinessCaseReq(BaseModel):
    save: bool = True


class WhatIfReq(BaseModel):
    region: Optional[str] = None
    sizing: Optional[str] = None      # as_is / measured / right_size
    byol: Optional[bool] = None


def _portfolio_inputs():
    """Gather persisted servers + matches + the strategies overlay (AI
    app_strategies + F2 tag-driven retain/retire) for portfolio costing."""
    from ..core.cost import strategies_from_tags
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    strategies = {s.app_id: s for s in STORE.list_app_strategies()}
    # operator tag-driven retain/retire wins over the AI strategy
    for app_id, strat in strategies_from_tags(servers).items():
        strategies[app_id] = strat
    return servers, matches, strategies


@app.post("/api/business-case")
def business_case(req: BusinessCaseReq):
    """Compute the portfolio TCO + per-strategy rollup (F2). Optionally save
    an immutable snapshot to ``business_case_snapshots``."""
    from ..core.cost import estimate_portfolio, load_pricebook
    from ..core.models import _new_id
    servers, matches, strategies = _portfolio_inputs()
    book = load_pricebook(settings)
    payload = estimate_portfolio(servers, matches, book, strategies=strategies)
    if req.save:
        sid = _new_id("bc")
        STORE.save_business_case(sid, payload)
        payload["snapshot_id"] = sid
    return payload


@app.get("/api/business-case")
def latest_business_case():
    """Return the most recently saved business-case snapshot, or 404."""
    snaps = STORE.list_business_cases(limit=1)
    if not snaps:
        raise HTTPException(404, "no saved business case; POST /api/business-case first")
    snap = STORE.get_business_case(snaps[0]["id"])
    return snap


@app.get("/api/business-case/{snapshot_id}")
def get_business_case_snapshot(snapshot_id: str):
    snap = STORE.get_business_case(snapshot_id)
    if not snap:
        raise HTTPException(404, "no such business-case snapshot")
    return snap


@app.post("/api/cost/what-if")
def cost_what_if(req: WhatIfReq):
    """Re-price the portfolio under alternate assumptions (F3 driver).

    Composable overrides (no re-ingest): ``region`` re-prices in a target
    region, ``sizing`` (as_is/measured/right_size) re-runs the rule engine to
    right-size from utilization, ``byol`` is the license-mode hint. Returns
    baseline + alternate + delta."""
    from ..core.cost import load_pricebook, what_if
    servers, matches, strategies = _portfolio_inputs()
    book = load_pricebook(settings)
    return what_if(servers, matches, book, region=req.region,
                   sizing=req.sizing, byol=req.byol, strategies=strategies)


class RightSizeReq(BaseModel):
    server_id: str
    strategy: str = "measured"     # as_is / measured / right_size


class ReRegionReq(BaseModel):
    server_id: str
    region: str


def _one_server_match(server_id: str):
    """Fetch a single persisted server + its match for per-server what-if."""
    s = next((x for x in STORE.list_all_servers() if x.id == server_id), None)
    if not s:
        raise HTTPException(404, f"no such server: {server_id}")
    m = next((x for x in STORE.list_matches() if x.server_id == server_id), None)
    return s, m


@app.post("/api/what-if/right-size")
def what_if_right_size(req: RightSizeReq):
    """Per-server what-if (F3): re-right-size one host under ``strategy`` and
    return the new target + the run-cost delta vs the persisted match. The
    copilot tab / agent calls this to answer "re-right-size this host as-is"
    without re-running ingest."""
    from ..core.cost import estimate_server, load_pricebook
    from ..core.match import right_size
    s, m = _one_server_match(req.server_id)
    book = load_pricebook(settings)
    new_match = right_size(s, strategy=req.strategy)
    new_est = estimate_server(s, new_match, book)
    # baseline from the persisted match (re-priced, not read from m.cost which
    # is only hydrated on rebuild) so the delta reflects pricing, not hydration.
    base_yearly = estimate_server(s, m, book).yearly_usd if m else 0.0
    return {
        "server_id": s.id, "hostname": s.hostname, "strategy": req.strategy,
        "before": {"product": m.target.product if m else None,
                   "spec": m.target.spec if m else None,
                   "region": m.target.region if m else None,
                   "yearly": round(base_yearly, 2)},
        "after": {"product": new_match.target.product,
                  "spec": new_match.target.spec,
                  "region": new_match.target.region,
                  "yearly": round(new_est.yearly_usd, 2)},
        "delta_yearly": round(new_est.yearly_usd - base_yearly, 2),
        "rationale": new_match.rationale,
    }


@app.post("/api/what-if/re-region")
def what_if_re_region(req: ReRegionReq):
    """Per-server what-if (F3): re-price one host as if it landed in ``region``
    and return the new target + run-cost delta. Same product/spec, region/az
    swapped — no re-ingest."""
    from ..core.cost import estimate_server, load_pricebook
    from ..core.match import re_region
    s, m = _one_server_match(req.server_id)
    book = load_pricebook(settings)
    new_match = re_region(s, req.region)
    new_est = estimate_server(s, new_match, book)
    base_yearly = estimate_server(s, m, book).yearly_usd if m else 0.0
    return {
        "server_id": s.id, "hostname": s.hostname, "region": req.region,
        "before": {"product": m.target.product if m else None,
                   "spec": m.target.spec if m else None,
                   "region": m.target.region if m else None,
                   "yearly": round(base_yearly, 2)},
        "after": {"product": new_match.target.product,
                  "spec": new_match.target.spec,
                  "region": new_match.target.region,
                  "yearly": round(new_est.yearly_usd, 2)},
        "delta_yearly": round(new_est.yearly_usd - base_yearly, 2),
        "rationale": new_match.rationale,
    }


# ---------------------------------------------------------------------------
# F6 — migration execution state machine + validation gates
# ---------------------------------------------------------------------------
class AdvanceReq(BaseModel):
    to: str
    note: str = ""
    by: str = "operator"


class GateResultReq(BaseModel):
    name: str
    result: str        # pass | fail | skipped
    by: str = "operator"


class CompleteReq(BaseModel):
    by: str = "operator"
    note: str = ""


@app.post("/api/waves/{wave_id}/execute")
def launch_wave_exec(wave_id: str, kind: str = "host"):
    """Create a MigrationJob (status=planned) for each server in the wave (F6).
    Track-only mode: the operator / external tool performs the migration."""
    from ..core.execute import launch_wave
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    wls = STORE.list_workloads()
    jobs = launch_wave(STORE, wave_id, servers, matches, wls, kind=kind)
    return {"wave_id": wave_id, "launched": len(jobs), "jobs": [j.to_dict() for j in jobs]}


@app.get("/api/migration-jobs")
def list_migration_jobs(wave_id: Optional[str] = None, status: Optional[str] = None,
                        server_id: Optional[str] = None):
    return [j.to_dict() for j in STORE.list_migration_jobs(
        wave_id=wave_id, status=status, server_id=server_id)]


@app.get("/api/migration-jobs/{job_id}")
def get_migration_job(job_id: str):
    j = STORE.get_migration_job(job_id)
    if not j:
        raise HTTPException(404, "no such migration job")
    return j.to_dict()


@app.post("/api/migration-jobs/{job_id}/advance")
def advance_migration_job(job_id: str, req: AdvanceReq):
    from ..core.execute import advance, InvalidTransition, GatesNotSatisfied
    j = STORE.get_migration_job(job_id)
    if not j:
        raise HTTPException(404, "no such migration job")
    try:
        advance(j, req.to, store=STORE, by=req.by, note=req.note)
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    except GatesNotSatisfied as e:
        raise HTTPException(409, str(e))
    return j.to_dict()


@app.post("/api/migration-jobs/{job_id}/revert")
def revert_migration_job(job_id: str, req: AdvanceReq):
    from ..core.execute import revert, InvalidTransition
    j = STORE.get_migration_job(job_id)
    if not j:
        raise HTTPException(404, "no such migration job")
    try:
        revert(j, req.to, store=STORE, by=req.by, note=req.note)
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    return j.to_dict()


@app.post("/api/migration-jobs/{job_id}/validate")
def validate_migration_job(job_id: str):
    """Run the job's validation gates; persist results onto the job."""
    from ..core.execute import run_validation_gates
    j = STORE.get_migration_job(job_id)
    if not j:
        raise HTTPException(404, "no such migration job")
    ok, _ = run_validation_gates(j, settings)
    STORE.upsert_migration_job(j)
    return {"job_id": job_id, "all_must_pass_ok": ok, "gates": j.validation_gates}


@app.post("/api/migration-jobs/{job_id}/gate")
def set_gate_result(job_id: str, req: GateResultReq):
    """Manually set a gate result (for manual gates the operator must confirm)."""
    j = STORE.get_migration_job(job_id)
    if not j:
        raise HTTPException(404, "no such migration job")
    if req.result not in ("pass", "fail", "skipped"):
        raise HTTPException(400, "result must be pass|fail|skipped")
    g = next((x for x in j.validation_gates if x.get("name") == req.name), None)
    if not g:
        raise HTTPException(404, f"no gate named {req.name}")
    g["result"] = req.result
    g["result_by"] = req.by
    g["result_at"] = _now()
    STORE.upsert_migration_job(j)
    return j.to_dict()


@app.post("/api/migration-jobs/{job_id}/complete")
def complete_migration_job(job_id: str, req: CompleteReq = None):
    """Manual mark-complete (operator confirms migration done + gates satisfied)."""
    from ..core.execute import complete, InvalidTransition
    j = STORE.get_migration_job(job_id)
    if not j:
        raise HTTPException(404, "no such migration job")
    by = (req.by if req else "operator")
    note = (req.note if req else "")
    try:
        complete(j, store=STORE, by=by, note=note)
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    return j.to_dict()


# ---------------------------------------------------------------------------
# F7 — wave execution rollup + handoff manifest
# ---------------------------------------------------------------------------
@app.get("/api/waves/{wave_id}/execution")
def wave_execution(wave_id: str):
    """Per-wave execution rollup: member MigrationJob states + dominant status."""
    from ..core.execute import wave_execution_status
    return wave_execution_status(STORE, wave_id)


@app.get("/api/waves/{wave_id}/handoff.csv")
def wave_handoff_csv(wave_id: str):
    """Assessment→execution handoff manifest (CSV): wave×workload×target×tool×status.
    Importable into the downstream runner (Tencent SMS / executor trigger)."""
    import csv, io
    from ..core.execute import handoff_rows
    rows = handoff_rows(STORE, wave_id)
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=wave-{wave_id}-handoff.csv"})


# ---------------------------------------------------------------------------
# F8 — Landing Zone parameterized blueprint + archetype placement gate
# ---------------------------------------------------------------------------
@app.get("/api/lz/archetypes")
def lz_archetypes():
    """The LZ archetype blueprints (corp/online/dmz → Tencent VPC/CAM/SG/tag
    policy) + per-server classification over the current estate (F8)."""
    from ..core.lz import LZ_ARCHETYPES, LZ_BLUEPRINTS, archetypes_for_servers
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    cls = archetypes_for_servers(servers, matches)
    counts = {a: 0 for a in LZ_ARCHETYPES}
    per_server = []
    for s in servers:
        a = cls.get(s.id, "corp")
        counts[a] = counts.get(a, 0) + 1
        per_server.append({"server_id": s.id, "hostname": s.hostname,
                           "role": s.role, "archetype": a})
    return {"archetypes": {a: LZ_BLUEPRINTS[a] for a in LZ_ARCHETYPES},
            "counts": counts, "per_server": per_server}


@app.get("/api/lz/readiness")
def lz_readiness_endpoint():
    """Per-archetype LZ readiness (F8): is each archetype's LZ finalized, from
    the persisted lz_status. Feeds the wave-launch placement gate + F10
    readiness heatmap."""
    from ..core.lz import lz_readiness
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    return lz_readiness(STORE, servers, matches)


class LZStatusReq(BaseModel):
    status: str          # not_ready / applied / finalized
    by: str = "operator"


@app.post("/api/lz/{archetype}/status")
def set_lz_status(archetype: str, req: LZStatusReq):
    """Mark an LZ archetype's lifecycle status (F8). ``finalized`` unblocks
    workload waves targeting that archetype (the placement gate)."""
    from ..core.lz import LZ_STATUSES
    if req.status not in LZ_STATUSES:
        raise HTTPException(400, f"status must be one of {LZ_STATUSES}")
    if archetype not in ("corp", "online", "dmz"):
        raise HTTPException(400, "archetype must be corp / online / dmz")
    STORE.set_lz_status(archetype, req.status, updated_by=req.by)
    return {"archetype": archetype, "status": req.status, "updated": True}


@app.get("/api/waves/{wave_id}/lz-gate")
def wave_lz_gate(wave_id: str):
    """Placement gate for a wave (F8): is the target archetype's LZ ready?
    Returns {ok, blocking_archetypes, archetype_mix, reason}."""
    from ..core.lz import check_lz_gate
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    return check_lz_gate(STORE, wave_id, servers, matches)


# ---------------------------------------------------------------------------
# F10 — portfolio readiness heatmap
# ---------------------------------------------------------------------------
@app.get("/api/readiness")
def readiness_portfolio():
    """Per-wave readiness heatmap (F10): {signals, rollup, can_cutover} for
    every wave. Answers "is this wave safe to cut?" — red blocks cutover."""
    from ..core.readiness import portfolio_readiness
    return portfolio_readiness(STORE)


@app.get("/api/readiness/{wave_id}")
def readiness_wave(wave_id: str):
    """One wave's readiness heatmap (F10)."""
    from ..core.readiness import wave_readiness
    out = wave_readiness(STORE, wave_id)
    if "error" in out:
        raise HTTPException(404, out["error"])
    return out


@app.get("/api/data-gaps")
def data_gaps():
    """Portfolio data-quality / information-blindspot report (data-gap).

    Rolls the per-host F4 assessment-confidence + key-field provenance + live-
    utilization + code-profile + warranty signals up to a portfolio view so the
    operator sees the blind spots (脱保 / unknown role / no util telemetry / no
    profile) in one place. The shadow-IT discovery diff (Gap1) is a separate
    endpoint (``/api/discovery/diff``) surfaced on the same Data Quality tab."""
    from ..core.datagaps import portfolio_data_gaps
    return portfolio_data_gaps(STORE)


@app.post("/api/discovery/scan")
def discovery_scan():
    """Run the shadow-IT / CMDB-drift discovery scan (Gap1).

    Compares a discovery snapshot (``IDC_DISCOVERY_PATH`` — a JSON list of hosts
    seen on the network / in vCenter / cloud drift) against the CMDB servers
    and persists the diff as the latest snapshot. Returns the diff
    (unknown_hosts / cmdb_orphans / drifted). Empty diff when
    ``IDC_DISCOVERY_PATH`` is unset (the default off state)."""
    from ..core.ingest.discovery import discover_drift
    diff = discover_drift(settings, STORE.list_all_servers())
    STORE.save_discovery(diff, created_at=diff.get("scanned_at", ""))
    return diff


@app.get("/api/discovery/diff")
def discovery_diff():
    """Return the latest persisted shadow-IT / CMDB-drift diff (Gap1).

    404 when no scan has been run yet. The Data Quality tab calls this on open
    so it shows the last scan without re-running the source; POST
    /api/discovery/scan refreshes it."""
    snap = STORE.get_discovery()
    if not snap:
        raise HTTPException(404, "no discovery scan run yet (POST /api/discovery/scan)")
    payload = snap["payload"] or {}
    payload.setdefault("scanned_at", snap.get("created_at", ""))
    return payload


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
    # WS scopes bypass the HTTP auth middleware — verify the session cookie
    # before accepting (the browser sends it on the same-origin upgrade).
    if not _ws_session_authed(ws):
        await ws.close(code=1008, reason="authentication required")
        return
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


@app.get("/executor")
def _executor_spec():
    """Public, self-contained spec page for the external agent executor — a
    URL operators can hand to the executor (human or agent) so it knows the
    contract: actions, endpoints, push callbacks, schemas, auth, quality rules."""
    p = WEB_DIR / "executor.html"
    if p.exists():
        return FileResponse(str(p), media_type="text/html")
    return JSONResponse({"error": "web/executor.html not built yet"}, status_code=404)


# ---------------------------------------------------------------------------
# Web auth — login / logout / change password
# ---------------------------------------------------------------------------
def _login_page(error: str = "") -> str:
    msg = f'<p class="err">{error}</p>' if error else '<p class="hint">Enter the operator password to access idc-migrate.</p>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>idc-migrate · login</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--red:#f85149}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:28px 28px 24px;width:340px;max-width:90vw}}
h1{{font-size:18px;margin:0 0 2px}} .sub{{color:var(--muted);margin:0 0 16px;font-size:12.5px}}
label{{display:block;margin:10px 0 4px;font-size:12.5px;color:var(--muted)}}
input{{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--fg);
border-radius:6px;padding:9px 10px;font-size:14px;outline:none}}
input:focus{{border-color:var(--accent)}}
button{{margin-top:16px;width:100%;background:var(--accent);color:#0d1117;border:0;border-radius:6px;
padding:10px;font-size:14px;font-weight:600;cursor:pointer}}
button:hover{{filter:brightness(1.08)}} .err{{color:var(--red);font-size:12.5px;margin:0 0 8px}}
.hint{{color:var(--muted);font-size:12.5px;margin:0 0 8px}}
</style></head><body>
<form class="card" method="post" action="/login">
<h1>idc-migrate</h1><p class="sub">IDC → Tencent Cloud copilot</p>
{msg}
<label for="password">Password</label>
<input id="password" name="password" type="password" autofocus required autocomplete="current-password"/>
<button type="submit">Log in</button>
</form></body></html>"""


@app.get("/login")
def login_get():
    # public (in _PUBLIC_PATHS); serve the form. If auth is off, bounce to /.
    if not _web_auth_password():
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_login_page())


@app.post("/login")
def login_post(request: Request, password: str = Form(...)):
    pw = _web_auth_password()
    if not pw:  # auth off
        return RedirectResponse("/", status_code=303)
    ip = (request.client.host if request.client else "") or "?"
    if _login_throttled(ip):
        return HTMLResponse(_login_page("Too many attempts — wait a minute."),
                            status_code=429)
    if secrets.compare_digest(password, pw):
        request.session["authed"] = True
        _LOGIN_FAILS.pop(ip, None)
        return RedirectResponse("/", status_code=303)
    _record_login_fail(ip)
    return HTMLResponse(_login_page("Wrong password."), status_code=401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/api/auth/password")
def change_password(request: Request, password: str = Form(...),
                    current: str = Form(...)):
    """Change the login password. Requires a logged-in session (middleware) AND
    the current password (re-verified) — so a hijacked session alone can't lock
    out the operator. Persists to system_config so it survives a restart and
    overrides the env IDC_WEB_PASSWORD."""
    pw = _web_auth_password()
    if not pw or not secrets.compare_digest(current, pw):
        raise HTTPException(401, "current password incorrect")
    if not password or len(password) < 4:
        raise HTTPException(400, "password must be at least 4 characters")
    STORE.set_config("web_password", password)
    return {"updated": True}


@app.on_event("shutdown")
def _shutdown():
    STORE.close()


def main():
    import uvicorn
    uvicorn.run("idc.backend.app:app", host="0.0.0.0", port=8010,
                reload=False, log_level="info")


if __name__ == "__main__":
    main()