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
from typing import Any, Dict, List, Literal, Optional

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
                           DocArtifact, DOC_TYPES, IaCArtifact, IAC_SCOPES,
                           LegacyDisposition, LEGACY_DISPOSITIONS,
                           LD_RETAIN, LD_RETIRE,
                           PostMigRecommendation, PM_KINDS,
                           TestCase, TestDiff, TestDiffItem, TestRun, TestResult,
                           TEST_KINDS, TEST_PHASES, TV_VERDICTS,
                           Question, ScanFinding, _now)
from ..llm import get_client
from ..agent import build_context, get_executor_client, run_agent
from ..agent import registry as _registry
from ..core.codeintel import app_targets, build_change_spec, resolve_value

# the single operator-managed executor id (registry.DEFAULT_ID) — referenced by
# the default-executor status/config views that report pull-mode connectivity.
DEFAULT_EXECUTOR_ID = _registry.DEFAULT_ID
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
    # any registered executor may push back to idc-migrate (each has its own
    # token); accept the bearer if it matches one of them.
    from ..agent import registry
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    tok = authorization.split(" ", 1)[1].strip()
    return any(secrets.compare_digest(tok, t)
               for t in registry.valid_tokens(STORE, settings))


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
# /api/executors/register is public on purpose: an executor self-enrolls
# before it has a bearer or a browser session. It is gated by the optional
# IDC_ENROLL_SECRET (checked in the endpoint), and every enrollment lands
# pending + inert until an operator approves — so open registration can only
# add pending rows, never claim work or push.
_PUBLIC_PATHS = {"/login", "/logout", "/executor", "/favicon.ico",
                 "/api/executors/register"}
# Paths where the executor bearer token is honored (the executor contract
# surface: push callbacks, read-only context pulls, and pull-dispatch). Everywhere
# else under /api/ requires a browser session — so a leaked bearer can't change
# the password, rewrite executor config, trigger scans, or drive migration jobs.
_BEARER_PREFIXES = ("/api/code-profiles", "/api/db-profiles", "/api/change-jobs",
                    "/api/workloads/", "/api/apps/", "/api/questions",
                    # F5/F7/F9/F10/F11 executor push-back callbacks (PUT scoped).
                    # Trailing slash keeps the list (GET) endpoints session-only
                    # for the browser while letting the executor's bearer auth
                    # through on the scoped PUT routes it pushes to.
                    "/api/legacy-dispositions/", "/api/docs/", "/api/iac-artifacts/",
                    "/api/postmig-recs/", "/api/test-cases/", "/api/test-runs/",
                    "/api/test-diffs/",
                    # pull-dispatch: the executor pulls work + reports completion
                    # (it exposes nothing, so this is its only way in).
                    "/api/executor/tasks")


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


async def _nocache_assets(request, call_next):
    """Force browsers to revalidate /assets/* (JS/CSS). StaticFiles sends only
    Last-Modified/ETag, no Cache-Control, so browsers heuristically cache the
    old copy and skip revalidation — which means a freshly-edited page module
    (e.g. business-case.js) stays invisible until a hard reload. no-cache +
    must-revalidate keeps a 304 path when unchanged but always fetches fresh
    bytes the moment a file changes. Auth-gated API routes are untouched."""
    resp = await call_next(request)
    if request.url.path.startswith("/assets/") or \
       resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


app.add_middleware(BaseHTTPMiddleware, dispatch=_nocache_assets)

# ---------------------------------------------------------------------------
# Live executor config — operator-editable at runtime via /api/executor/config
# ---------------------------------------------------------------------------
# The executor connection is env-driven by default (IDC_EXECUTOR_URL/TOKEN/
# ENABLED/TIMEOUT), but the web "Manage executor" panel can override it without
# a redeploy. Overrides persist in the system_config table so they survive
# restart; on startup we layer DB values over the env defaults.
_EXEC_TRUTHY = ("1", "true", "yes", "on")


def _executor_settings(executor_id: Optional[str] = None):
    """Resolve the selected executor's Settings: env ``IDC_EXECUTOR_*`` overlaid
    with the DB config (operator-managed via /api/executor/config for the
    default, /api/executors/{id} for named ones). ``executor_id`` None/''/
    'default' → the default executor; a named id → that registry entry (raises
    KeyError if absent). ``public_url`` is global (one push target for every
    executor). Read live on every call — no stale cache — so test monkeypatches
    of ``settings.executor_*`` and runtime DB PUTs both take effect instantly."""
    from ..agent import registry
    return registry.resolve(STORE, settings, executor_id)


def _resolve_executor(executor_id: Optional[str]):
    """Same as _executor_settings but maps a missing named executor to a 404
    and a still-PENDING self-enrollment to a 409 'pending approval' (used by
    every trigger endpoint — a pending executor can't be targeted)."""
    try:
        return _executor_settings(executor_id)
    except KeyError:
        from ..agent import registry
        eid = (executor_id or "").strip()
        if eid and any(e["id"] == eid and e.get("status") == registry.STATUS_PENDING
                       for e in registry.list_executors(STORE, settings)):
            raise HTTPException(409, f"executor {executor_id!r} pending approval")
        raise HTTPException(404, f"no such executor: {executor_id!r}")


def _now_iso() -> str:
    """ISO-8601 UTC ``...Z`` timestamp (codebase convention)."""
    from datetime import datetime
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _enqueue(kind: str, payload: Dict[str, Any],
             executor_id: Optional[str]) -> Dict[str, Any]:
    """Enqueue a task for an executor to pull (the executor exposes nothing, so
    idc-migrate cannot push to it — see docs/agent-executor.md). ``executor_id``
    None/''/'default' → the pool (any executor may claim); a named id targets
    that executor. ``payload`` carries the work + the feedback ``callback`` URL
    (built from IDC_PUBLIC_URL; empty → the executor falls back to its own
    IDC_CALLBACK_BASE). Returns ``{task_id, job_id, status:"pending"}``
    (``job_id`` is an alias for back-compat with the old push-trigger response)."""
    from ..core.models import _new_id
    from ..core.db import TASK_KINDS
    if kind not in TASK_KINDS:
        raise ValueError(f"unknown task kind: {kind!r}")
    eid = (executor_id or "").strip()
    # empty -> pool (any registered executor may claim); "default" or a named
    # id targets that executor specifically.
    tid = _new_id("tsk")
    STORE.enqueue_task(tid, kind, payload, eid or None, _now_iso())
    return {"task_id": tid, "job_id": tid, "status": "pending"}

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

# Hard cap on a single upload so a huge (or malicious) file can't fill the disk.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB

# Serialize full-estate rebuilds: two concurrent POST /api/rebuild would both
# DELETE+INSERT the derived tables, racing on row locks / deadlocking and
# leaving matches pointing at servers that no longer exist. Hold this lock for
# the whole rebuild; a second caller gets 409.
import threading as _threading
_REBUILD_LOCK = _threading.Lock()
_REBUILD_RUNNING = False


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
    mode: Literal["plan", "execute"] = "plan"   # execute = --dangerously-skip-permissions
    focus_server_ids: Optional[List[str]] = None
    cwd: Optional[str] = None
    timeout: Optional[int] = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _server_out(s, profiles_by_app=None, strategies_by_app=None,
                dispositions_by_key=None, book=None):
    d = s.to_dict()
    m = STORE.get_match(s.id)
    d["match"] = m.to_dict() if m else None
    # data-gap — precomputed support buckets so the inventory list + drawer can
    # render warranty / OS-EOL badges without duplicating the EOL table in JS.
    # Prefer the persisted bucket (B1 gap-actionable); fall back to computing
    # for stale rows that predate the column.
    d["warranty_bucket"] = s.warranty_bucket or warranty_bucket(s)
    d["os_eol_bucket"] = s.os_eol_bucket or os_eol_bucket(s)
    # 7R policy for this host — single source of truth shared with the Business
    # case + CLI (cost.seven_r_for): host-level retain/retire override wins over
    # the app-level AI strategy, else rehost. Only computed when the caller
    # preloaded the (small) strategies map; the paginated list preloads it once
    # so this is O(1) per row, not a DB round-trip per host.
    if strategies_by_app is not None:
        from ..core.cost import seven_r_for, host_disposition_for
        dk = dispositions_by_key or {}
        # operator / executor host disposition (retain / retire / "") — the raw
        # override the drawer's Disposition row shows + edits. Keyed by the
        # host's stable identity (hostname lowercased), survives a Rebuild.
        d["disposition"] = host_disposition_for(s, dk.get)
        d["seven_r"] = seven_r_for(s, strategies_by_app, dk.get)
        # the source of the 7R policy, so the drawer can show WHY (host override
        # / which app's AI strategy / the rehost default). Mirrors the precedence
        # in seven_r_for exactly.
        if d["disposition"]:
            d["seven_r_source"] = "host override"
        else:
            src_app = next((a for a in (s.app_ids or []) if a in strategies_by_app), None)
            d["seven_r_source"] = (f"app AI · {src_app}" if src_app else "default (rehost)")
        # code-scan enrichment: only when a profile map is supplied (single-
        # server detail). The paginated list endpoint does NOT pass one, so it
        # stays fast and does not load all profiles per row.
        if profiles_by_app is not None:
            d["code"] = _server_code_summary(s, profiles_by_app, strategies_by_app)
            # host-level extras for the drawer (detail path only): the waves the
            # host belongs to (scan list_waves — 643 waves is fine for one host)
            # + the run-cost estimate (when a pricebook is passed in). The list
            # path skips both — waves is a scan, cost needs the book.
            d["waves"] = [{"id": w.id, "name": w.name, "stage": w.stage}
                          for w in STORE.list_waves() if s.id in (w.server_ids or [])]
            if book is not None and m is not None:
                from ..core.cost import estimate_server
                est = estimate_server(s, m, book)
                d["cost"] = {"monthly": round(est.monthly_usd, 2),
                             "yearly": round(est.yearly_usd, 2),
                             "product": est.target_product, "spec": est.spec,
                             "region": est.region, "basis": est.basis,
                             "pricing_source": est.pricing_source}
            else:
                d["cost"] = None
    else:
        d["disposition"] = ""
        d["seven_r"] = ""
        d["seven_r_source"] = ""
    return d


def _first_code_profile(s):
    """The first code profile attached to one of the server's apps, or None.
    Shared by the drawer's LLM actions (explain / right-size / audit) so they
    reason over the app's runtime / framework / blockers, not just the host."""
    if not s.app_ids:
        return None
    pmap = {p.app_id: p for p in STORE.list_code_profiles()}
    return next((pmap[a] for a in s.app_ids if a in pmap), None)


def _host_disposition_keys(s) -> List[str]:
    """The stable-identity keys a host disposition is stored under (hostname
    lowercased, identity_key, server id) — the same keys ``non_migrating_servers``
    matches legacy dispositions by, so the drawer reads the SAME row the planner
    honors. First non-empty wins as the lookup key."""
    return [k for k in (s.hostname.lower().strip(), getattr(s, "identity_key", ""),
                        s.id) if k]


def _host_disposition(s) -> str:
    """Current retain/retire disposition for a host, or "" (migrate)."""
    for k in _host_disposition_keys(s):
        d = STORE.get_legacy_disposition(k)
        if d and d.disposition in (LD_RETAIN, LD_RETIRE):
            return d.disposition
    return ""


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
            # path A — optional codex pass output (agent (codex) grounded). Kept
            # compact for the drawer summary: a count + the blockers (capped) +
            # a short summary. The full agent_findings list is on the Scan &
            # Migrate page; the drawer just flags that the agent found something.
            "agent_findings_count": len(p.agent_findings or []),
            "agent_blockers": list(p.agent_blockers or [])[:5],
            "agent_summary": (p.agent_summary or "")[:200],
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
                 app_id: Optional[str] = None,
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
                     warranty_bucket=warranty_bucket, os_eol_bucket=os_eol_bucket,
                     app_id=app_id)
    res = STORE.query_servers(f, page=page, page_size=min(page_size, 500),
                              order_by=order_by, order_dir=order_dir,
                              with_facets=facets)
    # preload the small strategy + disposition maps ONCE (not per row) so each
    # row's 7R policy is an O(1) lookup — the Inventory "7R" column reads the
    # same source of truth as the Business case + drawer.
    strategies_by_app = {s2.app_id: s2 for s2 in STORE.list_app_strategies()}
    dispositions_by_key = {d.server_id: d.disposition
                            for d in STORE.list_legacy_dispositions()}
    items = [_server_out(s, strategies_by_app=strategies_by_app,
                         dispositions_by_key=dispositions_by_key)
             for s in res["items"]]
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
    dk = {d.server_id: d.disposition for d in STORE.list_legacy_dispositions()}
    from ..core.cost import load_pricebook
    book = load_pricebook(settings)
    return _server_out(s, profiles_by_app=pmap, strategies_by_app=smap,
                       dispositions_by_key=dk, book=book)


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


@app.put("/api/servers/{sid}/disposition")
def put_server_disposition(sid: str, body: dict):
    """Operator per-host disposition: retain (keep on-prem) or retire
    (decommission), or "" to clear (migrate with the waves). Set from the
    inventory host drawer.

    Stored as a ``LegacyDisposition`` keyed by the host's STABLE identity
    (hostname lowercased) — the SAME store the executor's EOL analysis pushes
    to and the SAME row ``non_migrating_servers`` reads, so the operator's call
    survives a Rebuild (separate table) and drives the trailing Retain / Retire
    waves on the next plan. No executor bearer — this is a browser-session
    operator action, like the warranty override.

    A host-level disposition overrides the app-level 7R rule: a retain/retire
    host is pulled out of every app wave even when a migrating app claims it
    (the app migrates with its OTHER hosts). Hosts with no app binding are also
    eligible (a standalone host can be retired)."""
    s = STORE.get_server(sid)
    if not s:
        raise HTTPException(404, "server not found")
    disp = (body.get("disposition") or "").strip().lower()
    if disp and disp not in (LD_RETAIN, LD_RETIRE):
        raise HTTPException(400, f"disposition must be '{LD_RETAIN}' or '{LD_RETIRE}' or empty")
    key = _host_disposition_keys(s)[0] if _host_disposition_keys(s) else sid
    if not disp:
        STORE.delete_legacy_disposition(key)
        return {"server_id": sid, "disposition": "", "cleared": True}
    d = LegacyDisposition(
        server_id=key, disposition=disp,
        rationale=(body.get("rationale") or "operator override").strip(),
        confidence=1.0, summary=f"operator {disp} from inventory host drawer",
    )
    STORE.upsert_legacy_disposition(d)
    return {"server_id": sid, "disposition": disp, "key": key, "updated": True}


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
    written = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = await file.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                fh.close()
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise HTTPException(413, f"upload too large (limit {MAX_UPLOAD_BYTES} bytes)")
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
    """Validate the bearer token against ANY registered executor's token (live
    config, so a token change via /api/executor/config or /api/executors/{id}
    takes effect immediately). With multiple executors configured, each pushes
    back with its own token; idc-migrate accepts any of them."""
    from ..agent import registry
    tokens = registry.valid_tokens(STORE, settings)
    if not tokens:
        raise HTTPException(401, "IDC_EXECUTOR_TOKEN not configured; "
                                  "set it to accept executor push callbacks")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    tok = authorization.split(" ", 1)[1].strip()
    if not any(secrets.compare_digest(tok, t) for t in tokens):
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


@app.get("/api/apps")
def list_apps():
    """The full app catalog (app_id + name + tier + env) for UI dropdowns —
    every ``app_id`` input on the Code & DB page picks from this list instead
    of being typed freehand. Backed by ``workloads`` (covers apps even with no
    servers yet); ordered by app_id."""
    return STORE.app_options()


# ---------------------------------------------------------------------------
# Repos (git urls) — first-class source object, N:N with hosts. An app's repos
# are DERIVED from its hosts (app->hosts via workloads, then hosts->repos via
# host_repos); the repo->apps reverse mapping is derived here from servers'
# app_ids. Phase 1: entity + N:N host mapping + management UI. The code-profile
# re-key to repo_id (so a repo is scanned once, profile per repo) is Phase 2.
# ---------------------------------------------------------------------------
def _repo_name_from_url(url: str) -> str:
    """Best-effort human label from a git url: last path segment, .git stripped."""
    u = (url or "").strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    if "://" in u:
        u = u.split("://", 1)[1]
    seg = u.rsplit("/", 1)[-1] if "/" in u else u
    seg = seg.split("@", 1)[-1]  # strip any user@ prefix
    return seg or (url or "").strip()


@app.get("/api/repos")
def list_repos_endpoint():
    """All repos, enriched with the hostnames that deploy each and the apps
    derived from those hosts' app_ids. Ordered by url.

    Only the hosts actually linked to a repo are read (one JOIN over
    host_repos x servers) — NOT the whole estate. The old code called
    list_all_servers() (SELECT * FROM servers + 6 JSON parses per row) just to
    look up hostnames, which made this endpoint crawl on a 13K-server estate."""
    from ..core.models import Repo  # noqa: F401 (clarifies the row shape)
    repos = STORE.list_repos()
    enrich = STORE.repo_host_enrichment()   # repo_id -> [{server_id,hostname,app_ids}]
    out = []
    for r in repos:
        hs = enrich.get(r.repo_id, [])
        hostnames = [h["hostname"] for h in hs]
        apps = sorted({a for h in hs for a in h["app_ids"]})
        out.append({
            "repo_id": r.repo_id, "url": r.url, "branch": r.branch or "",
            "name": r.name or _repo_name_from_url(r.url),
            "hosts": hostnames, "host_count": len(hostnames),
            "apps": apps, "created_at": r.created_at, "updated_at": r.updated_at,
        })
    return out


class RepoReq(BaseModel):
    url: str
    branch: str = ""
    name: Optional[str] = None


@app.post("/api/repos")
def create_repo(req: RepoReq):
    """Register a git url as a first-class repo. url is UNIQUE — a second add
    of the same url 409s (the existing row is the shared repo). mints repo_id."""
    from ..core.models import Repo, _new_id, _now
    url = (req.url or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")
                       or url.startswith("git@") or url.startswith("ssh://")):
        raise HTTPException(400, "url must be a git http(s)/ssh url")
    if STORE.get_repo_by_url(url):
        raise HTTPException(409, "a repo with that url already exists (it is shared — link it to hosts instead)")
    repo = Repo(repo_id=_new_id("repo"), url=url, branch=(req.branch or "").strip(),
                name=(req.name or "").strip() or _repo_name_from_url(url),
                created_at=_now(), updated_at=_now())
    STORE.upsert_repo(repo)
    return {"repo_id": repo.repo_id, "url": repo.url, "branch": repo.branch,
            "name": repo.name, "created_at": repo.created_at}


@app.put("/api/repos/{repo_id}")
def update_repo(repo_id: str, req: RepoReq):
    """Edit a repo's url/branch/name. A url change to one already used by
    another repo 409s."""
    from ..core.models import _now
    r = STORE.get_repo(repo_id)
    if not r:
        raise HTTPException(404, f"no such repo: {repo_id!r}")
    url = (req.url or r.url).strip()
    if not url:
        raise HTTPException(400, "url must not be empty")
    other = STORE.get_repo_by_url(url)
    if other and other.repo_id != repo_id:
        raise HTTPException(409, "another repo already uses that url")
    r.url = url
    r.branch = (req.branch or r.branch).strip()
    r.name = (req.name or r.name or _repo_name_from_url(url)).strip()
    r.updated_at = _now()
    STORE.upsert_repo(r)
    return {"repo_id": r.repo_id, "url": r.url, "branch": r.branch, "name": r.name,
            "updated_at": r.updated_at}


@app.delete("/api/repos/{repo_id}")
def delete_repo_endpoint(repo_id: str):
    """Delete a repo and cascade its host_repos edges."""
    if not STORE.delete_repo(repo_id):
        raise HTTPException(404, f"no such repo: {repo_id!r}")
    return {"repo_id": repo_id, "deleted": True}


@app.get("/api/repos/{repo_id}/hosts")
def repo_hosts(repo_id: str):
    """The hosts (server_id + hostname + fqdn) that deploy this repo. One JOIN
    over host_repos x servers — does NOT load the whole estate (the old code did
    list_all_servers() just to resolve one repo's hostnames)."""
    if not STORE.get_repo(repo_id):
        raise HTTPException(404, f"no such repo: {repo_id!r}")
    return STORE.repo_hosts_enriched(repo_id)


class RepoHostsReq(BaseModel):
    hosts: List[str] = []   # free-text: server_id, hostname, or fqdn (case-insensitive)


@app.put("/api/repos/{repo_id}/hosts")
def set_repo_hosts_endpoint(repo_id: str, req: RepoHostsReq):
    """Replace the set of hosts that deploy a repo (the N:N edge, repo side).
    Accepts free-text tokens (server_id / hostname / fqdn); unmatched tokens
    are reported back so the operator can fix typos."""
    if not STORE.get_repo(repo_id):
        raise HTTPException(404, f"no such repo: {repo_id!r}")
    res = STORE.resolve_server_ids(req.hosts)
    STORE.set_repo_hosts(repo_id, res["matched"])
    return {"repo_id": repo_id, "matched": len(res["matched"]),
            "unresolved": res["unresolved"]}


@app.get("/api/hosts/{server_id}/repos")
def host_repos_endpoint(server_id: str):
    """The repos deployed on a host (the N:N edge, host side). The path token
    may be a server_id, hostname, or fqdn (case-insensitive) — resolved to a
    server_id so the operator can type a hostname. 404 if it matches no host."""
    from ..core.models import Repo  # noqa: F401
    sid = _resolve_host_token(server_id)
    rids = STORE.host_repos_for(sid)
    if not rids:
        return []
    by_id = {r.repo_id: r for r in STORE.list_repos()}
    return [{"repo_id": r.repo_id, "url": r.url, "branch": r.branch,
             "name": r.name or _repo_name_from_url(r.url)}
            for r in (by_id[rid] for rid in rids if rid in by_id)]


@app.get("/api/hosts/suggest")
def suggest_hosts(q: str = "", limit: int = 20):
    """Lightweight hostname typeahead for the repo chips editor and any host
    picker: ``[{server_id, hostname, fqdn, role}]`` matching a substring on
    hostname/fqdn/ips/tags/app_ids. Deliberately returns NO per-row match/EOL
    enrichment (unlike ``/api/servers``, which does a get_match per item) so it
    stays fast keystroke-by-keystroke. Empty ``q`` -> the first ``limit`` hosts
    by hostname. ``limit`` capped at 50."""
    f = ServerFilter(q=(q or None))
    res = STORE.query_servers(f, page=1, page_size=min(limit, 50),
                              order_by="hostname", order_dir="asc",
                              with_facets=False)
    return [{"server_id": s.id, "hostname": s.hostname or s.id,
             "fqdn": s.fqdn or "", "role": s.role or ""}
            for s in res["items"]]


class HostReposReq(BaseModel):
    repo_ids: List[str] = []   # repos to deploy on this host (replaces the set)


@app.put("/api/hosts/{server_id}/repos")
def set_host_repos_endpoint(server_id: str, req: HostReposReq):
    """Replace the set of repos a host deploys (the N:N edge, host side). The
    path token may be a server_id / hostname / fqdn (resolved). repo_ids that
    don't exist are reported as unresolved (the link isn't created for them)."""
    sid = _resolve_host_token(server_id)
    known = {r.repo_id for r in STORE.list_repos()}
    valid = [rid for rid in dict.fromkeys(req.repo_ids) if rid in known]
    unresolved = [rid for rid in req.repo_ids if rid not in known]
    STORE.set_host_repos(sid, valid)
    return {"server_id": sid, "linked": len(valid), "unresolved": unresolved}


def _resolve_host_token(token: str) -> str:
    """Resolve a free-text host token (server_id / hostname / fqdn) to a
    server_id. 404 if it matches no existing host."""
    if not token:
        raise HTTPException(404, "no such host")
    # fast path: it's already a real server_id
    if STORE.get_server(token):
        return token
    res = STORE.resolve_server_ids([token])
    if res["matched"]:
        return res["matched"][0]
    raise HTTPException(404, f"no such host: {token!r}")


@app.get("/api/apps/{app_id}/repos")
def app_repos_endpoint(app_id: str):
    """An app's repos, DERIVED from its hosts (app->hosts via workloads, then
    hosts->repos via host_repos). Not stored — recomputed on read."""
    from ..core.models import Repo  # noqa: F401
    rids = STORE.repos_for_app(app_id)
    if not rids:
        return []
    by_id = {r.repo_id: r for r in STORE.list_repos()}
    return [{"repo_id": r.repo_id, "url": r.url, "branch": r.branch,
             "name": r.name or _repo_name_from_url(r.url)}
            for r in (by_id[rid] for rid in rids if rid in by_id)]


@app.get("/api/apps/{app_id}/sources")
def app_sources_endpoint(app_id: str):
    """An app's sources, enriched with WHICH of the app's hosts carry each — the
    app-centric primary view of the Code tab. app↔repo is derived from hosts, so
    this walks only the app's hosts (NOT the whole estate): for each host in the
    app's workload, collect its repos, then label the carrying hostnames via a
    targeted IN-query. 404 if the app has no workload. Returns
    ``[{repo_id, url, name, branch, hostnames, host_count}]`` + ``host_total``."""
    from ..core.models import Repo  # noqa: F401
    w = next((x for x in STORE.list_workloads() if x.app_id == app_id), None)
    if not w:
        raise HTTPException(404, f"no such app: {app_id!r}")
    sids = w.server_ids or []
    carry: Dict[str, List[str]] = {}        # repo_id -> [server_id] (app hosts)
    for sid in sids:
        for rid in STORE.host_repos_for(sid):
            carry.setdefault(rid, []).append(sid)
    labels = STORE.host_labels([s for hs in carry.values() for s in hs])
    by_id = {r.repo_id: r for r in STORE.list_repos()}
    out = []
    for rid, hsids in carry.items():
        r = by_id.get(rid)
        if not r:
            continue
        hostnames = [labels.get(s, s) for s in hsids]
        out.append({
            "repo_id": r.repo_id, "url": r.url, "branch": r.branch or "",
            "name": r.name or _repo_name_from_url(r.url),
            "hostnames": hostnames, "host_count": len(hostnames),
        })
    out.sort(key=lambda x: x["name"].lower())
    return {"app_id": app_id, "host_total": len(sids), "sources": out}


class AppRepoReq(BaseModel):
    repo_id: str
    action: Literal["add", "remove"] = "add"


@app.put("/api/apps/{app_id}/repos")
def set_app_repos(app_id: str, req: AppRepoReq):
    """Link/unlink a source to an app. app↔repo is DERIVED from hosts, so this
    adds (or removes) the repo to/from EVERY host in the app's workload — a
    per-host union/diff, NOT a replace (each host's other repos are preserved).
    Idempotent: a host that already has/doesn't-have the repo is skipped.
    Returns ``{app_id, repo_id, action, hosts_changed, hosts_skipped}``."""
    w = next((x for x in STORE.list_workloads() if x.app_id == app_id), None)
    if not w:
        raise HTTPException(404, f"no such app: {app_id!r}")
    if not STORE.get_repo(req.repo_id):
        raise HTTPException(404, f"no such repo: {req.repo_id!r}")
    changed = 0
    skipped = 0
    for sid in w.server_ids or []:
        cur = STORE.host_repos_for(sid)
        if req.action == "add":
            if req.repo_id in cur:
                skipped += 1
                continue
            STORE.set_host_repos(sid, list(cur) + [req.repo_id])
        else:
            if req.repo_id not in cur:
                skipped += 1
                continue
            STORE.set_host_repos(sid, [x for x in cur if x != req.repo_id])
        changed += 1
    return {"app_id": app_id, "repo_id": req.repo_id, "action": req.action,
            "hosts_changed": changed, "hosts_skipped": skipped}


# ---------------------------------------------------------------------------
# Repo discovery — the operator pastes a git GROUP/ORG url, the executor
# enumerates the repositories inside it and pushes the list back here, the
# operator picks which to register. idc-migrate never touches git itself (the
# repo-access contract, docs/agent-executor.md §2.0②): the executor does the
# SCM API / ls-remote work with its own credentials. One RepoScan row per scan.
# ---------------------------------------------------------------------------
class RepoDiscoverReq(BaseModel):
    url: str
    executor_id: Optional[str] = None


@app.post("/api/repos/discover")
def discover_repos(req: RepoDiscoverReq):
    """Enqueue a ``discover-repos`` task: an executor pulls it, enumerates the
    repositories inside the git group/org url, and pushes the list back to
    ``PUT /api/repos/discover/{scan_id}/result``. Returns
    ``{scan_id, task_id, status:"pending"}``. Async — poll the scan to read it."""
    from ..core.models import RepoScan, _new_id, _now
    url = (req.url or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")
                       or url.startswith("git@") or url.startswith("ssh://")):
        raise HTTPException(400, "url must be a git http(s)/ssh url")
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    scan_id = _new_id("scn")
    now = _now()
    STORE.upsert_repo_scan(RepoScan(
        scan_id=scan_id, url=url, status="pending",
        executor_id=(req.executor_id or "").strip(), created_at=now))
    payload = {"scan_id": scan_id, "url": url,
               "callback": ec.cb(f"/api/repos/discover/{scan_id}/result")}
    res = _enqueue("discover-repos", payload, req.executor_id)
    res["scan_id"] = scan_id
    res["url"] = url
    return res


@app.get("/api/repos/discover")
def list_repo_scans_endpoint():
    """Recent discovery scans (newest-first) — the UI's scan history."""
    return [s.to_dict() for s in STORE.list_repo_scans(limit=50)]


@app.get("/api/repos/discover/{scan_id}")
def get_repo_scan_endpoint(scan_id: str):
    """One discovery scan: status + the discovered repos (empty until done).
    The UI polls this until ``status`` flips to ``done``/``error``."""
    sc = STORE.get_repo_scan(scan_id)
    if not sc:
        raise HTTPException(404, f"no such scan: {scan_id!r}")
    return sc.to_dict()


class RepoDiscoverResultReq(BaseModel):
    # the discovered repositories: each {url, name, branch, description, web_url}
    repos: List[Dict[str, Any]] = []
    error: Optional[str] = None


@app.put("/api/repos/discover/{scan_id}/result")
def discover_repos_result(scan_id: str, req: RepoDiscoverResultReq,
                          authorization: Optional[str] = Header(None)):
    """Executor callback: store the discovered repos (or an error) and flip the
    scan to ``done``/``error``. Bearer auth — only a registered executor may
    push results back. 404 if the scan row doesn't exist."""
    _check_executor_auth(authorization)
    if not STORE.get_repo_scan(scan_id):
        raise HTTPException(404, f"no such scan: {scan_id!r}")
    eid = _caller_executor_id(authorization)
    status = "error" if (req.error and not req.repos) else "done"
    err = (req.error or "") if status == "error" else ""
    ok = STORE.complete_repo_scan(scan_id, status, req.repos, err, eid, _now_iso())
    if not ok:
        raise HTTPException(404, f"no such scan: {scan_id!r}")
    return {"scan_id": scan_id, "status": status, "count": len(req.repos)}


class RepoBulkReq(BaseModel):
    # repos to register: each {url, branch?, name?}. url must be present + git-shaped.
    repos: List[Dict[str, Any]] = []


@app.post("/api/repos/bulk")
def bulk_create_repos(req: RepoBulkReq):
    """Register many repos at once (idempotent): a url that already exists is
    skipped (it is the shared repo — link it to hosts instead). Used by the
    discovery UI's "register selected" after a scan. Returns
    ``{created: [...], skipped: [...], invalid: [...]}`` (urls)."""
    from ..core.models import Repo, _new_id, _now
    created: List[str] = []
    skipped: List[str] = []
    invalid: List[str] = []
    seen: set = set()
    for item in req.repos or []:
        url = (item.get("url") or "").strip()
        if not url or not (url.startswith("http://") or url.startswith("https://")
                           or url.startswith("git@") or url.startswith("ssh://")):
            invalid.append(url or "(empty)")
            continue
        if url in seen or STORE.get_repo_by_url(url):
            seen.add(url)
            skipped.append(url)
            continue
        seen.add(url)
        repo = Repo(repo_id=_new_id("repo"), url=url,
                    branch=(item.get("branch") or "").strip(),
                    name=(item.get("name") or "").strip() or _repo_name_from_url(url),
                    created_at=_now(), updated_at=_now())
        STORE.upsert_repo(repo)
        created.append(url)
    return {"created": created, "skipped": skipped, "invalid": invalid,
            "created_count": len(created)}


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
# Skill management — browse / edit / reset the idc-migrate LLM skills (7R /
# audit / assess / review / lz / network / waveplan). Editing writes a markdown
# file to the IDC_SKILLS_DIR overlay (never to the repo's idc/skills/); the
# loader re-scans on write so the change takes effect immediately, no restart.
# Operator-only: these are under /api/ and NOT in the public/bearer lists, so
# _auth_dispatch requires a browser session (web_password gate) — a leaked
# executor bearer can't rewrite LLM behavior. See idc/llm/skills.py.
# ---------------------------------------------------------------------------
@app.get("/api/skills")
def list_skills_api():
    from ..llm.skills import list_skill_infos
    return list_skill_infos(get_settings())


@app.get("/api/skills/{name}")
def get_skill_api(name: str):
    from ..llm.skills import skill_view
    s = skill_view(name, get_settings())
    if s is None:
        raise HTTPException(404, "no such skill")
    return s


@app.put("/api/skills/{name}")
def put_skill_api(name: str, body: dict):
    """Create or overwrite a skill's markdown in the IDC_SKILLS_DIR overlay.
    The body's ``markdown`` is the full file (--- frontmatter --- + body)."""
    from ..llm.skills import write_skill
    md = str(body.get("markdown") or "")
    if not md.strip():
        raise HTTPException(400, "markdown empty")
    try:
        path = write_skill(name, md, get_settings())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"name": name, "saved": True, "file_path": path}


@app.delete("/api/skills/{name}")
def delete_skill_api(name: str):
    """Remove the overlay file (reverts to the built-in file or the _FALLBACK
    constant). No-op (removed=False) if no overlay file exists."""
    from ..llm.skills import delete_skill
    removed = delete_skill(name, get_settings())
    return {"name": name, "removed": removed}


@app.get("/api/skills/{name}/files/{file_path:path}")
def get_skill_file_api(name: str, file_path: str):
    """Read one bundled reference/script file of a directory-form skill (round 1).
    Returns content/kind/size. 404 if the skill has no directory form or the path
    isn't a bundled file (the loader's file list is the allowlist — no traversal).
    Operator-only (same session gate as the rest of /api/skills)."""
    from ..llm.skills import read_skill_file
    res = read_skill_file(name, file_path, get_settings())
    if res is None:
        raise HTTPException(404, "no such skill file")
    content, kind, size = res
    return {"name": name, "path": file_path, "kind": kind,
            "size": size, "content": content}


@app.put("/api/skills/{name}/files/{file_path:path}")
def put_skill_file_api(name: str, file_path: str, body: dict):
    """Create or overwrite one bundled reference/script file (round 2). Promotes
    a single-file / builtin / fallback skill to directory-form overlay if needed.
    ``body.content`` is the file text. 400 if no overlay dir / invalid path /
    script not .py. Operator-only (same session gate as /api/skills)."""
    from ..llm.skills import write_skill_file
    content = str(body.get("content") or "")
    try:
        path = write_skill_file(name, file_path, content, get_settings())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"name": name, "path": file_path, "saved": True, "file_path": path}


@app.delete("/api/skills/{name}/files/{file_path:path}")
def delete_skill_file_api(name: str, file_path: str):
    """Remove one bundled file from a directory-form overlay skill (round 2).
    No-op (removed=False) if no overlay skill dir / the path isn't bundled.
    Operator-only (same session gate as /api/skills)."""
    from ..llm.skills import delete_skill_file
    removed = delete_skill_file(name, file_path, get_settings())
    return {"name": name, "path": file_path, "removed": removed}


@app.post("/api/skills/{name}/try")
def try_skill_api(name: str, body: dict):
    """Run a skill WITHOUT saving — the Skills-page Try panel. ``body.input`` is
    the free-text user message; optional ``body.markdown`` is an unsaved edit to
    try (parsed as a transient skill). Returns {ok, kind, output, error,
    resources_touched}. Structured-input skills (seven_r/audit/assess/review)
    reject with kind='wired'. Operator-only (same session gate as /api/skills)."""
    from ..llm.runner import try_skill
    user_input = str(body.get("input") or "")
    markdown = body.get("markdown") or None
    if markdown is not None:
        markdown = str(markdown)
    return try_skill(name, user_input, get_settings(), markdown=markdown)


@app.get("/api/skills/{name}/prev")
def get_skill_prev_api(name: str):
    """The previous-saved markdown snapshot for one-step undo (round 3). Returns
    {has_prev, markdown}. Operator-only (same session gate as /api/skills)."""
    from ..llm.skills import get_skill_prev
    md = get_skill_prev(name, get_settings())
    return {"name": name, "has_prev": md is not None, "markdown": md or ""}


@app.post("/api/skills/{name}/undo")
def undo_skill_api(name: str):
    """Restore the .prev snapshot as the current overlay (one-step undo, round 3).
    Returns {restored, markdown}. restored=False if no snapshot. Operator-only."""
    from ..llm.skills import undo_skill
    md = undo_skill(name, get_settings())
    return {"name": name, "restored": md is not None, "markdown": md or ""}


@app.post("/api/skills/{name}/run-script")
def run_skill_script_api(name: str, body: dict):
    """Run a skill's bundled python script (the editor's CURRENT content,
    unsaved OK) in a sandbox and return stdout/stderr/exit — the Skills-page
    Run button. ``body.path`` is ``scripts/<name>.py``; ``body.content`` is the
    script text to run (the textarea's current value, so you can debug before
    saving). Operator-only (same session gate as /api/skills — a leaked
    executor bearer can't run code on the box)."""
    from ..llm.runner import run_skill_script
    path = str(body.get("path") or "")
    content = str(body.get("content") or "")
    if not path:
        raise HTTPException(400, "path required (scripts/<name>.py)")
    return run_skill_script(name, path, content, get_settings())


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


# ---------------------------------------------------------------------------
# F7 — legacy / unsupported-OS dispositions (executor pushes; UI / agent reads)
# ---------------------------------------------------------------------------
@app.get("/api/legacy-dispositions")
def list_legacy_dispositions():
    """List all legacy-OS dispositions (read-only; keyed by stable host identity)."""
    return [d.to_dict() for d in STORE.list_legacy_dispositions()]


@app.get("/api/legacy-dispositions/{server_id}")
def get_legacy_disposition(server_id: str):
    d = STORE.get_legacy_disposition(server_id)
    if not d:
        raise HTTPException(404, f"no legacy disposition for {server_id}")
    return d.to_dict()


@app.put("/api/legacy-dispositions/{server_id}")
def put_legacy_disposition(server_id: str, body: dict,
                           authorization: Optional[str] = Header(None)):
    """Executor pushes/overwrites a LegacyDisposition for an EOL host (F7).

    ``server_id`` is the host's STABLE identity (hostname lowercased), like
    db-profiles. Bearer auth, same as the other push endpoints."""
    _check_executor_auth(authorization)
    if body.get("server_id") and body["server_id"] != server_id:
        raise HTTPException(400, "server_id in body must match path")
    body["server_id"] = server_id
    if body.get("disposition") and body["disposition"] not in LEGACY_DISPOSITIONS:
        raise HTTPException(400, f"disposition must be one of {LEGACY_DISPOSITIONS}")
    try:
        d = LegacyDisposition.from_dict(body)
    except Exception as e:
        raise HTTPException(400, f"invalid LegacyDisposition: {e!r}")
    STORE.upsert_legacy_disposition(d)
    return {"server_id": server_id, "updated": True}


@app.delete("/api/legacy-dispositions/{server_id}")
def delete_legacy_disposition(server_id: str, authorization: Optional[str] = Header(None)):
    _check_executor_auth(authorization)
    STORE.delete_legacy_disposition(server_id)
    return {"server_id": server_id, "deleted": True}


class LegacyDispositionReq(BaseModel):
    server_id: str
    context: Dict[str, Any] = {}
    executor_id: Optional[str] = None


@app.post("/api/legacy-disposition")
def legacy_disposition_trigger(req: LegacyDispositionReq):
    """Ask the external executor to analyze an EOL/unsupported-OS host and
    recommend containerize / re-platform / rewrite / retain (F7). The executor
    pushes a LegacyDisposition back to PUT /api/legacy-dispositions/{server_id};
    the next rebuild folds it into the host's match via apply_os_eol."""
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    payload = {"server_id": req.server_id, "context": req.context,
               "callback": ec.cb(f"/api/legacy-dispositions/{req.server_id}")}
    return _enqueue("legacy-disposition", payload, req.executor_id)


# ---------------------------------------------------------------------------
# F9 — generated doc artifacts: cutover playbook + as-built (+ runbook)
# ---------------------------------------------------------------------------
# doc_type is normalized so the path can be "as-built" (URL-friendly) while the
# stored doc_type is "as_built" (matches DOC_TYPES).
_DOC_SLUG_MAP = {"as-built": "as_built", "cutover": "cutover", "runbook": "runbook"}
_DOC_SLUG_REV = {v: k for k, v in _DOC_SLUG_MAP.items()}


def _doc_type_from_slug(slug: str) -> Optional[str]:
    return _DOC_SLUG_MAP.get(slug)


@app.get("/api/docs")
def list_doc_artifacts(doc_type: Optional[str] = None):
    """List generated doc artifacts (optionally filtered by doc_type)."""
    return [d.to_dict() for d in STORE.list_doc_artifacts(doc_type=doc_type)]


@app.get("/api/docs/{doc_type}/{scope_id}")
def get_doc_artifact(doc_type: str, scope_id: str):
    real = _doc_type_from_slug(doc_type) or doc_type
    d = STORE.get_doc_artifact(real, scope_id)
    if not d:
        raise HTTPException(404, f"no {doc_type} doc for {scope_id}")
    return d.to_dict()


@app.put("/api/docs/{doc_type}/{scope_id}")
def put_doc_artifact(doc_type: str, scope_id: str, body: dict,
                     authorization: Optional[str] = Header(None)):
    """Executor pushes/overwrites a generated DocArtifact (F9). Bearer auth."""
    _check_executor_auth(authorization)
    real = _doc_type_from_slug(doc_type) or doc_type
    if real not in DOC_TYPES:
        raise HTTPException(400, f"doc_type must be one of {DOC_TYPES}")
    body["doc_type"] = real
    body["scope_id"] = scope_id
    try:
        d = DocArtifact.from_dict(body)
    except Exception as e:
        raise HTTPException(400, f"invalid DocArtifact: {e!r}")
    STORE.upsert_doc_artifact(d)
    return {"doc_type": real, "scope_id": scope_id, "updated": True}


@app.delete("/api/docs/{doc_type}/{scope_id}")
def delete_doc_artifact(doc_type: str, scope_id: str,
                        authorization: Optional[str] = Header(None)):
    _check_executor_auth(authorization)
    real = _doc_type_from_slug(doc_type) or doc_type
    STORE.delete_doc_artifact(real, scope_id)
    return {"doc_type": real, "scope_id": scope_id, "deleted": True}


class DocGenReq(BaseModel):
    wave_id: str
    context: Dict[str, Any] = {}
    executor_id: Optional[str] = None


def _wave_doc_context(wave_id: str) -> Dict[str, Any]:
    """Assemble the real wave data the doc generators need (F9). The executor
    builds the markdown from THIS context — sending an empty context would
    produce an empty doc, so the backend (which has the store) builds it:
    members (server/role/target/ports/deps/reverse_replication), the downtime
    window, the deterministic risk basis, and the execution data (stage
    history, gate results, change jobs, final targets). Pure reads, no LLM.
    """
    from ..core.codeintel import wave_risk_basis
    wave = next((w for w in STORE.list_waves() if w.id == wave_id), None)
    if wave is None:
        return {"wave_id": wave_id, "members": [], "risk_basis": {}, "targets": []}
    servers = {s.id: s for s in STORE.list_all_servers()}
    matches = {m.server_id: m for m in STORE.list_matches()}
    workloads = STORE.list_workloads()
    profiles = STORE.list_code_profiles()
    db_profiles = STORE.list_db_profiles()
    srv_to_app = {}
    for w in workloads:
        for sid in (w.server_ids or []):
            srv_to_app.setdefault(sid, w.app_id)
    members = []
    for sid in (wave.server_ids or []):
        s = servers.get(sid)
        m = matches.get(sid)
        if not s:
            continue
        role = s.role or ""
        tgt = (m.target.product + " " + m.target.spec) if m and m.target else ""
        ports = []
        if m and m.target:
            p = m.target.extras.get("port")
            if p:
                ports.append(int(p))
        reverse_replication = False
        if role == "db":
            for k in (s.hostname.lower().strip(), getattr(s, "identity_key", ""), s.id):
                dbp = next((d for d in db_profiles if d.db_server_id == k), None)
                if dbp:
                    reverse_replication = bool(dbp.reverse_replication)
                    break
        members.append({"server_id": sid, "hostname": s.hostname, "role": role,
                        "target": tgt, "ports": ports, "deps": [],
                        "app_id": srv_to_app.get(sid, ""),
                        "reverse_replication": reverse_replication})
    risk = wave_risk_basis(wave, list(servers.values()), list(matches.values()),
                           profiles, db_profiles)
    return {"wave_id": wave_id, "members": members,
            "downtime_window": dict(wave.downtime_window or {}),
            "risk_basis": risk, "targets": [],
            "stage_history": [], "gate_results": [], "change_jobs": []}


def _wave_as_built_context(wave_id: str) -> Dict[str, Any]:
    """Execution data for the as-built doc (F9): per-server stage history,
    validation-gate results, change-job patch refs, and final matched
    targets. Layered on top of _wave_doc_context."""
    ctx = _wave_doc_context(wave_id)
    jobs = STORE.list_migration_jobs(wave_id=wave_id)
    stage_history, gate_results = [], []
    for j in jobs:
        for st in (j.stage_history or []):
            stage_history.append({"server_id": j.server_id, **st})
        for g in (j.validation_gates or []):
            gate_results.append({"server_id": j.server_id,
                                 "name": g.get("name"), "kind": g.get("kind"),
                                 "status": g.get("result"), "detail": g.get("detail")})
    change_jobs = [{"app_id": c.get("app_id"), "patch_ref": c.get("patch_ref"),
                    "status": c.get("status")}
                   for c in STORE.list_change_jobs(limit=500)
                   if c.get("kind") in ("modify", "comb")]
    # final targets (post-migration) from the matches
    servers = {s.id: s for s in STORE.list_all_servers()}
    matches = {m.server_id: m for m in STORE.list_matches()}
    tgt = []
    wv = next((w for w in STORE.list_waves() if w.id == wave_id), None)
    for sid in ((wv.server_ids if wv else []) or []):
        m = matches.get(sid)
        s = servers.get(sid)
        if m and m.target and s:
            tgt.append({"server_id": sid, "role": s.role or "",
                        "product": m.target.product, "spec": m.target.spec})
    ctx.update({"stage_history": stage_history, "gate_results": gate_results,
                "change_jobs": change_jobs, "targets": tgt})
    return ctx


@app.post("/api/cutover-playbook")
def cutover_playbook_trigger(req: DocGenReq):
    """Ask the executor to generate a per-wave cutover playbook (F9). The
    executor pushes a DocArtifact back to PUT /api/docs/cutover/{wave_id}."""
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    # F9 — the backend builds the real wave context (members / risk / downtime
    # window) so the executor's doc is grounded, not empty.
    ctx = req.context or _wave_doc_context(req.wave_id)
    payload = {"wave_id": req.wave_id, "context": ctx,
               "callback": ec.cb(f"/api/docs/cutover/{req.wave_id}")}
    return _enqueue("cutover-playbook", payload, req.executor_id)


@app.post("/api/as-built")
def as_built_trigger(req: DocGenReq):
    """Ask the executor to generate a per-wave as-built doc (F9). The executor
    pushes a DocArtifact back to PUT /api/docs/as-built/{wave_id}."""
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    ctx = req.context or _wave_as_built_context(req.wave_id)
    payload = {"wave_id": req.wave_id, "context": ctx,
               "callback": ec.cb(f"/api/docs/as-built/{req.wave_id}")}
    return _enqueue("as-built", payload, req.executor_id)


# ---------------------------------------------------------------------------
# F5 — IaC artifacts (landing-zone/workload Terraform + guardrail checks)
# ---------------------------------------------------------------------------
@app.get("/api/iac-artifacts")
def list_iac_artifacts(scope: Optional[str] = None):
    """List stored IaC artifacts (optionally filtered by scope)."""
    return [a.to_dict() for a in STORE.list_iac_artifacts(scope=scope)]


@app.get("/api/iac-artifacts/{scope_id}")
def get_iac_artifact(scope_id: str):
    a = STORE.get_iac_artifact(scope_id)
    if not a:
        raise HTTPException(404, f"no IaC artifact for {scope_id}")
    return a.to_dict()


@app.put("/api/iac-artifacts/{scope_id}")
def put_iac_artifact(scope_id: str, body: dict,
                     authorization: Optional[str] = Header(None)):
    """Executor pushes/overwrites an IaCArtifact (F5). Bearer auth."""
    _check_executor_auth(authorization)
    if body.get("scope_id") and body["scope_id"] != scope_id:
        raise HTTPException(400, "scope_id in body must match path")
    body["scope_id"] = scope_id
    if body.get("scope") and body["scope"] not in IAC_SCOPES:
        raise HTTPException(400, f"scope must be one of {IAC_SCOPES}")
    try:
        a = IaCArtifact.from_dict(body)
    except Exception as e:
        raise HTTPException(400, f"invalid IaCArtifact: {e!r}")
    STORE.upsert_iac_artifact(a)
    return {"scope_id": scope_id, "updated": True, "guardrail_pass": a.guardrail_pass}


@app.delete("/api/iac-artifacts/{scope_id}")
def delete_iac_artifact(scope_id: str,
                        authorization: Optional[str] = Header(None)):
    _check_executor_auth(authorization)
    STORE.delete_iac_artifact(scope_id)
    return {"scope_id": scope_id, "deleted": True}


class IacEmitReq(BaseModel):
    scope: str
    scope_id: str
    context: Dict[str, Any] = {}
    executor_id: Optional[str] = None


@app.post("/api/iac-emit")
def iac_emit_trigger(req: IacEmitReq):
    """Ask the executor to emit IaC + run guardrail checks (F5). ``scope`` is
    landing_zone (scope_id = lz:<arch>, context = blueprint) or workload
    (scope_id = wl:<server_id>, context = match). The executor pushes an
    IaCArtifact back to PUT /api/iac-artifacts/{scope_id}; check_lz_gate then
    blocks workload launch until guardrails pass."""
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    if req.scope not in IAC_SCOPES:
        raise HTTPException(400, f"scope must be one of {IAC_SCOPES}")
    payload = {"scope": req.scope, "scope_id": req.scope_id, "context": req.context,
               "callback": ec.cb(f"/api/iac-artifacts/{req.scope_id}")}
    return _enqueue("iac-emit", payload, req.executor_id)


# ---------------------------------------------------------------------------
# F10 — post-migration optimization recs (right_size/reserved/anomaly/perf)
# ---------------------------------------------------------------------------
@app.get("/api/postmig-recs")
def list_postmig_recs(server_id: Optional[str] = None):
    """List post-mig recommendations (optionally filtered by server)."""
    return [r.to_dict() for r in STORE.list_postmig_recs(server_id=server_id)]


@app.get("/api/postmig-recs/{server_id}/{kind}")
def get_postmig_rec(server_id: str, kind: str):
    r = STORE.get_postmig_rec(server_id, kind)
    if not r:
        raise HTTPException(404, f"no {kind} rec for {server_id}")
    return r.to_dict()


@app.put("/api/postmig-recs/{server_id}/{kind}")
def put_postmig_rec(server_id: str, kind: str, body: dict,
                    authorization: Optional[str] = Header(None)):
    """Executor pushes/overwrites a PostMigRecommendation (F10). Bearer auth."""
    _check_executor_auth(authorization)
    if kind not in PM_KINDS:
        raise HTTPException(400, f"kind must be one of {PM_KINDS}")
    body["server_id"] = server_id
    body["kind"] = kind
    try:
        r = PostMigRecommendation.from_dict(body)
    except Exception as e:
        raise HTTPException(400, f"invalid PostMigRecommendation: {e!r}")
    STORE.upsert_postmig_rec(r)
    return {"server_id": server_id, "kind": kind, "updated": True}


@app.delete("/api/postmig-recs/{server_id}")
def delete_postmig_recs(server_id: str,
                        authorization: Optional[str] = Header(None)):
    _check_executor_auth(authorization)
    STORE.delete_postmig_recs(server_id)
    return {"server_id": server_id, "deleted": True}


@app.get("/api/postmig-savings")
def postmig_savings():
    """Total realized monthly/yearly savings across all post-mig recs (F10)."""
    from ..core.cost import postmig_savings as _sum
    return _sum(STORE.list_postmig_recs())


class PostMigReq(BaseModel):
    server_id: str
    context: Dict[str, Any] = {}
    executor_id: Optional[str] = None


@app.post("/api/postmig-optimize")
def postmig_optimize_trigger(req: PostMigReq):
    """Ask the executor to analyze a finalized host's post-mig metrics (F10).
    The executor pushes PostMigRecommendation records back to
    PUT /api/postmig-recs/{server_id}/{kind}."""
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    # postmig pushes one PostMigRecommendation per kind to
    # /api/postmig-recs/{server_id}/{kind} — the {kind} isn't known at trigger
    # time, so the per-task callback (a single full path) can't cover it. Left
    # empty; the executor uses its own callback-base env (IDC_CALLBACK_BASE).
    payload = {"server_id": req.server_id, "context": req.context, "callback": ""}
    return _enqueue("postmig-optimize", payload, req.executor_id)


# ---------------------------------------------------------------------------
# F11 — automated testing (test cases / runs / diffs + triggers)
# ---------------------------------------------------------------------------
@app.get("/api/test-cases")
def list_test_cases(app_id: Optional[str] = None):
    return [c.to_dict() for c in STORE.list_test_cases(app_id=app_id)]


@app.get("/api/test-cases/{app_id}")
def get_test_cases(app_id: str):
    return [c.to_dict() for c in STORE.list_test_cases(app_id=app_id)]


@app.put("/api/test-cases/{app_id}")
def put_test_cases(app_id: str, body: dict,
                   authorization: Optional[str] = Header(None)):
    """Executor pushes a LIST of TestCase records for an app (F11). Bearer auth.
    Replaces the app's cases with the pushed list (gen is a full re-gen)."""
    _check_executor_auth(authorization)
    cases = body.get("cases") or []
    # validate BEFORE the tx so a bad record doesn't leave a partial state
    parsed: list = []
    for c in cases:
        c["app_id"] = app_id
        try:
            tc = TestCase.from_dict(c)
        except Exception as e:
            raise HTTPException(400, f"invalid TestCase: {e!r}")
        if tc.kind and tc.kind not in TEST_KINDS:
            raise HTTPException(400, f"kind must be one of {TEST_KINDS}")
        parsed.append(tc)
    out = []
    # clear + re-insert atomically: the whole swap is one transaction so a crash
    # mid-loop can't delete an app's cases and only partially re-add them.
    with STORE.tx() as cur:
        cur.execute(STORE._x("DELETE FROM test_cases WHERE app_id=?"), (app_id,))
        for tc in parsed:
            cur.execute(*STORE._upsert_test_case_sql(tc))
            out.append(tc.name)
    return {"app_id": app_id, "cases": out, "updated": True}


@app.get("/api/test-runs")
def list_test_runs(app_id: Optional[str] = None):
    return [r.to_dict() for r in STORE.list_test_runs(app_id=app_id)]


@app.get("/api/test-runs/{run_id}")
def get_test_run(run_id: str):
    r = STORE.get_test_run(run_id)
    if not r:
        raise HTTPException(404, f"no test run {run_id}")
    return r.to_dict()


@app.put("/api/test-runs/{run_id}")
def put_test_run(run_id: str, body: dict,
                authorization: Optional[str] = Header(None)):
    """Executor pushes a TestRun (F11). Bearer auth."""
    _check_executor_auth(authorization)
    body["id"] = run_id
    if body.get("phase") and body["phase"] not in TEST_PHASES:
        raise HTTPException(400, f"phase must be one of {TEST_PHASES}")
    try:
        r = TestRun.from_dict(body)
    except Exception as e:
        raise HTTPException(400, f"invalid TestRun: {e!r}")
    STORE.upsert_test_run(r)
    return {"run_id": run_id, "updated": True}


@app.get("/api/test-diffs")
def list_test_diffs():
    return [d.to_dict() for d in STORE.list_test_diffs()]


@app.get("/api/test-diffs/{app_id}")
def get_test_diff(app_id: str):
    d = STORE.get_test_diff(app_id)
    if not d:
        raise HTTPException(404, f"no test diff for {app_id}")
    return d.to_dict()


@app.put("/api/test-diffs/{app_id}")
def put_test_diff(app_id: str, body: dict,
                  authorization: Optional[str] = Header(None)):
    """Executor pushes a TestDiff (F11). Bearer auth. Drives the
    test_regression cutover gate (regressions > 0 blocks finalize)."""
    _check_executor_auth(authorization)
    body["app_id"] = app_id
    try:
        d = TestDiff.from_dict(body)
    except Exception as e:
        raise HTTPException(400, f"invalid TestDiff: {e!r}")
    STORE.upsert_test_diff(d)
    return {"app_id": app_id, "regressions": d.regressions, "updated": True}


class TestGenReqAPI(BaseModel):
    app_id: str
    context: Dict[str, Any] = {}
    executor_id: Optional[str] = None


@app.post("/api/test-gen")
def test_gen_trigger(req: TestGenReqAPI):
    """Ask the executor to generate test cases (F11). The executor pushes the
    cases back to PUT /api/test-cases/{app_id}."""
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    payload = {"app_id": req.app_id, "context": req.context,
               "callback": ec.cb(f"/api/test-cases/{req.app_id}")}
    return _enqueue("test-gen", payload, req.executor_id)


class TestRunReqAPI(BaseModel):
    app_id: str
    phase: str
    target: str
    context: Dict[str, Any] = {}
    executor_id: Optional[str] = None


@app.post("/api/test-run")
def test_run_trigger(req: TestRunReqAPI):
    """Enqueue a test-run task (F11). phase = pre (baseline) / post (after
    cutover). The executor pulls the task and pushes a TestRun back to
    PUT /api/test-runs/{run_id} (using its own IDC_CALLBACK_BASE)."""
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    if req.phase not in TEST_PHASES:
        raise HTTPException(400, f"phase must be one of {TEST_PHASES}")
    # pre-mint the run_id so idc-migrate knows which TestRun the async push will
    # land under before the executor's callback arrives (contract §1.11). Baked
    # into the task payload; the executor pushes back under it.
    from ..core.models import _new_id
    run_id = _new_id("trun")
    # test-run pushes to /api/test-runs/{run_id} (IDC_CALLBACK_BASE — {run_id} is
    # in the path, not a fixed per-task callback), so callback is left empty.
    payload = {"app_id": req.app_id, "phase": req.phase, "target": req.target,
               "context": req.context, "run_id": run_id, "callback": ""}
    res = _enqueue("test-run", payload, req.executor_id)
    res["run_id"] = run_id
    return res


class TestCompareReqAPI(BaseModel):
    app_id: str
    pre_run_id: str
    post_run_id: str
    executor_id: Optional[str] = None


@app.post("/api/test-compare")
def test_compare_trigger(req: TestCompareReqAPI):
    """Ask the executor to diff pre vs post (F11). The executor pushes a
    TestDiff back to PUT /api/test-diffs/{app_id}; the test_regression gate
    then blocks finalize on regressions."""
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    payload = {"app_id": req.app_id, "pre_run_id": req.pre_run_id,
               "post_run_id": req.post_run_id,
               "callback": ec.cb(f"/api/test-diffs/{req.app_id}")}
    return _enqueue("test-compare", payload, req.executor_id)


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
    # which named executor to drive (None / "default" → the default executor).
    executor_id: Optional[str] = None
    # for modify: operator-supplied old→new value map (e.g.
    # {"10.0.4.20": "${DB_HOST}", "hunter2": "${DB_PASSWORD}"}). idc-migrate
    # folds these into the concrete change list it builds from the app's
    # CodeProfile, so the executor is told exactly what to replace.
    overrides: Optional[Dict[str, str]] = None


@app.get("/api/executor/status")
def executor_status_endpoint():
    """Pull-mode status for the default executor — powers the web status
    indicator + the `doctor` CLI check. idc-migrate never probes, so
    `reachable` is None; the live signal is `connected`/`last_seen` from the
    executor's inbound poll activity on the queue. Never raises."""
    from ..agent import executor_status
    return _merge_activity(executor_status(_executor_settings()), DEFAULT_EXECUTOR_ID)


def _merge_activity(status: Dict[str, Any], executor_id: str) -> Dict[str, Any]:
    """Overlay pull-mode connectivity (inbound activity) onto a static
    ``executor_status()`` dict. idc-migrate never probes, so ``reachable``
    stays None and the live signal is ``connected``/``last_seen`` from the
    queue heartbeats. Used by /api/executor/status + /api/executor/config
    (the default executor); named executors get the same shape via
    _executor_with_status."""
    status.update(_activity_status(executor_id))
    return status


class ExecutorConfigReq(BaseModel):
    # All optional — only provided fields are updated on PUT; /test reports a
    # candidate {url, token} pull-mode status without persisting.
    url: Optional[str] = None
    token: Optional[str] = None
    enabled: Optional[bool] = None
    timeout: Optional[int] = None
    public_url: Optional[str] = None


def _executor_config_out(s=None) -> dict:
    """Public view of the executor config (token NEVER returned — only token_set)
    plus pull-mode connectivity (inbound activity — no outbound probe)."""
    from ..agent import executor_status
    s = s or _executor_settings()
    status = _merge_activity(executor_status(s), DEFAULT_EXECUTOR_ID)
    return {
        "url": s.executor_url or "",
        "token_set": bool(s.executor_token),
        "enabled": bool(s.executor_enabled),
        "timeout": int(s.executor_timeout or 0),
        "public_url": s.public_url or "",
        "status": status,
    }


@app.get("/api/executor/config")
def executor_get_config():
    """Current executor connection (URL / enabled / timeout / token_set) plus
    pull-mode connectivity. The token itself is never returned."""
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
    if req.public_url is not None:
        u = req.public_url.strip()
        if u and not (u.startswith("http://") or u.startswith("https://")):
            raise HTTPException(400, "public_url must be http(s)://...")
        STORE.set_config("public_url", u)
    return _executor_config_out()


@app.post("/api/executor/test")
def executor_test_config(req: ExecutorConfigReq):
    """Report pull-mode status for a CANDIDATE {url, token} WITHOUT persisting.
    idc-migrate never reaches out to the executor, so there is no probe — this
    just layers the provided url/token over the live config (falling back to
    live values for fields not supplied) and returns the static pull-mode
    status (``reachable`` is always None). Kept for back-compat; the UI no
    longer exposes a "Test connection" button (it can't — idc-migrate never
    initiates a connection)."""
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


# ---------------------------------------------------------------------------
# Named-executor registry — drive N external executors, not just the default.
# The default (id="default") is managed via /api/executor/config above; named
# ones via this surface. All push back to the same idc-migrate public URL and
# authenticate with their own token (any registered token validates).
# ---------------------------------------------------------------------------
# Pull mode: idc-migrate never initiates a connection to any executor, so a
# row's "connected" dot is NOT a probe result — it is "this executor polled
# idc-migrate (claim/lease/complete) within the window". An executor that polls
# continuously touches idc-migrate every few seconds, so 2x the claim lease is
# a generous freshness threshold; an executor past it shows "idle · last seen".
EXECUTOR_CONNECTED_WINDOW_S = 1800   # 2x EXECUTOR_LEASE_SECONDS (15min)


def _activity_status(executor_id: str) -> Dict[str, Any]:
    """Pull-mode connectivity for one executor, derived purely from inbound
    activity (no outbound probe). ``connected`` means the executor touched
    idc-migrate within EXECUTOR_CONNECTED_WINDOW_S. Never raises — a missing
    executor_id yields a never-seen status."""
    from datetime import datetime, timezone
    act = STORE.executor_activity(executor_id) if executor_id else \
        {"last_seen": None, "in_flight": 0, "done": 0, "error": 0}
    last = act.get("last_seen")
    connected = False
    age = None
    if last:
        try:
            ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
            age = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
            connected = age <= EXECUTOR_CONNECTED_WINDOW_S
        except Exception:
            connected = False
    if last and connected:
        detail = f"connected · last poll {age}s ago"
    elif last:
        detail = f"idle · last poll {age}s ago"
    else:
        detail = "registered · no poll yet"
    return {
        # back-compat: pull mode never probes, so reachable stays None (not
        # a True/False probe result — the UI keys off `connected` instead).
        "reachable": None,
        "connected": connected,
        "last_seen": last,
        "in_flight": int(act.get("in_flight", 0)),
        "done": int(act.get("done", 0)),
        "error": int(act.get("error", 0)),
        "detail": detail,
    }


def _executor_with_status(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Public registry entry: strip the token, attach pull-mode connectivity
    (inbound activity, not an outbound probe — idc-migrate never reaches out).
    A PENDING self-enrollment has no token and never polls, so it surfaces
    'pending approval' with no last_seen."""
    from ..agent import registry
    out = registry.public_view(entry)
    if out.get("approval") == registry.STATUS_PENDING:
        out["status"] = {"reachable": None, "connected": False, "last_seen": None,
                         "in_flight": 0, "done": 0, "error": 0,
                         "detail": "pending approval"}
        return out
    out["status"] = _activity_status(entry["id"])
    return out


@app.get("/api/executors")
def list_executors_endpoint():
    """All configured executors (default first) with per-executor pull-mode
    connectivity (inbound poll activity — idc-migrate never probes). Token is
    never returned (only ``token_set``)."""
    from ..agent import registry
    return [_executor_with_status(e)
            for e in registry.list_executors(STORE, settings)]


class ExecutorUpsertReq(BaseModel):
    # All optional except url; only provided fields change on update. Token is
    # kept as-is when not supplied (so a re-save to toggle enabled doesn't drop
    # the secret — same masking convention as the default executor).
    url: Optional[str] = None
    token: Optional[str] = None
    enabled: Optional[bool] = None
    timeout: Optional[int] = None


@app.put("/api/executors/{executor_id}")
def upsert_executor(executor_id: str, req: ExecutorUpsertReq):
    """Add or update a NAMED executor (id != default). The default executor is
    managed via /api/executor/config, not here. Persists to the ``executors``
    JSON config row; takes effect immediately (read live)."""
    from ..agent import registry
    eid = executor_id.strip()
    if eid == registry.DEFAULT_ID:
        raise HTTPException(400, "the default executor is managed via /api/executor/config")
    # build the entry from the existing one (if any) overlaid with provided fields
    existing = next((e for e in registry.list_executors(STORE, settings)
                     if e["id"] == eid), None) or {"id": eid, "url": "", "token": "",
                                                    "enabled": True, "timeout": 600}
    entry = dict(existing)
    if req.url is not None:
        entry["url"] = req.url
    if req.token is not None and req.token != "":
        entry["token"] = req.token
    if req.enabled is not None:
        entry["enabled"] = req.enabled
    if req.timeout is not None:
        entry["timeout"] = req.timeout
    try:
        saved = registry.upsert(STORE, entry)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _executor_with_status(saved)


@app.delete("/api/executors/{executor_id}")
def delete_executor(executor_id: str):
    """Remove a NAMED executor (the default can't be deleted). Serves both as
    'remove' for an approved executor and 'reject' for a pending self-enrollment."""
    from ..agent import registry
    try:
        removed = registry.delete(STORE, executor_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not removed:
        raise HTTPException(404, f"no such executor: {executor_id!r}")
    return {"id": executor_id, "deleted": True}


# ---------------------------------------------------------------------------
# Self-registration → approval lifecycle. An executor enrolls itself
# (POST /api/executors/register, public + optional enroll-secret gate); it
# lands PENDING + inert. An operator approves it (POST /api/executors/{id}/approve),
# at which point idc-migrate MINTS the bearer token and returns it ONCE so the
# operator can relay it to the executor out-of-band (Option B: the executor
# never holds a secret it invented). rotate-token re-issues an approved
# executor's token (e.g. the one-time token was lost).
# ---------------------------------------------------------------------------
class ExecutorRegisterReq(BaseModel):
    id: str
    url: Optional[str] = None
    timeout: Optional[int] = None


@app.post("/api/executors/register")
def register_executor(req: ExecutorRegisterReq,
                      x_enroll_secret: Optional[str] = Header(None, alias="X-Enroll-Secret")):
    """Executor self-enrollment (public; no bearer/session — the executor has
    neither yet). Optional IDC_ENROLL_SECRET gate: when set (env or DB), the
    request must carry X-Enroll-Secret matching. Always lands the executor
    PENDING with no token; an operator must approve it before it can claim or
    push. Idempotent on a pending id; rejected (409) for an already-approved id."""
    from ..agent import registry
    sec = registry.enroll_secret(STORE, settings)
    if sec and not secrets.compare_digest((x_enroll_secret or "").strip(), sec):
        raise HTTPException(403, "enrollment secret required or mismatched")
    try:
        entry = registry.register(STORE, {
            "id": req.id, "url": req.url, "timeout": req.timeout,
        })
    except ValueError as e:
        # already-approved id → 409; bad id/url → 400
        status_code = 409 if "already approved" in str(e) else 400
        raise HTTPException(status_code, str(e))
    # return the public view (no token) with the pending status probe
    return _executor_with_status(entry)


@app.post("/api/executors/{executor_id}/approve")
def approve_executor(executor_id: str):
    """Operator approves a PENDING self-enrollment. Mints the executor's bearer
    token server-side and returns it ONCE under ``token`` (the operator relays
    it to the executor out-of-band). The token is never returned again — only
    ``token_set`` thereafter; use rotate-token to re-issue. Operator-only
    (browser session)."""
    from ..agent import registry
    try:
        entry = registry.approve(STORE, executor_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # surface the freshly-minted token exactly once
    out = registry.public_view(entry)
    out["token"] = entry["token"]
    out["status"] = {"reachable": None, "detail": "approved — token shown once"}
    return out


@app.post("/api/executors/{executor_id}/rotate-token")
def rotate_executor_token(executor_id: str):
    """Operator re-issues an APPROVED named executor's token (the old one is
    invalidated). Returns the new token ONCE under ``token``. Use when the
    one-time approval token was lost or a secret is suspected leaked.
    Operator-only (browser session)."""
    from ..agent import registry
    try:
        entry = registry.rotate_token(STORE, executor_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = registry.public_view(entry)
    out["token"] = entry["token"]
    out["status"] = {"reachable": None, "detail": "token rotated — shown once"}
    return out


@app.post("/api/executors/{executor_id}/test")
def test_executor(executor_id: str):
    """Report pull-mode status for one executor (no persistence, no outbound
    probe — idc-migrate never reaches out). Kept for back-compat; the UI shows
    connectivity via the inbound-activity dot, not a Test button."""
    from ..agent import registry, executor_status
    try:
        s = registry.resolve(STORE, settings, executor_id)
    except KeyError:
        raise HTTPException(404, f"no such executor: {executor_id!r}")
    return executor_status(s)


@app.post("/api/executor/trigger")
def executor_trigger(req: ExecutorScanReq):
    """Enqueue a scan/comb/modify task for an executor to pull (async).

    Returns ``{task_id, job_id, status:"pending"}``. An executor pulls the task
    via ``POST /api/executor/tasks/claim`` and pushes results back into
    ``PUT /api/code-profiles/{app_id}`` / ``POST /api/change-jobs`` when done.

    For ``modify``: idc-migrate builds a concrete change list from the app's
    stored ``CodeProfile`` (joined with the operator ``overrides``) and bakes it
    into the task payload, so the executor knows *which file/line, which
    literal, which value* — it does not re-scan or guess. If no profile exists
    yet the change list is empty and the spec's ``notes`` tell the executor to
    scan first.
    """
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    if req.action == "modify":
        profile = STORE.get_code_profile(req.app_id)
        # F9 — runtime-derived profiles (no source) must be operator-confirmed
        # before the executor writes the inferred scaffold. In plan mode the
        # scaffold is previewed freely; in execute mode the confirm-gate fires
        # (a Question is raised; the operator answers, then re-triggers).
        from ..core.codeintel import is_runtime_derived, runtime_confirm_question, SOURCE_RUNTIME
        if is_runtime_derived(profile) and req.mode == "execute":
            # The confirm-gate is a one-shot: once the operator has answered
            # "confirmed — proceed with modify" we must NOT raise 409 again
            # (otherwise every re-trigger mints a new question and the
            # operator can never reach the enqueue).
            already_confirmed = any(
                q.app_id == req.app_id
                and (q.context or {}).get("source") == SOURCE_RUNTIME
                and q.status == "answered"
                and str(q.answer or "").lower().startswith("confirmed")
                for q in STORE.list_questions(app_id=req.app_id)
            )
            if not already_confirmed:
                q = runtime_confirm_question(req.app_id, profile)
                if q is not None:
                    STORE.upsert_question(q)
                    raise HTTPException(409, f"runtime-derived scaffold requires "
                                               f"operator confirmation (question {q.id}); "
                                               f"answer it then re-trigger modify")
        spec = build_change_spec(profile, scope=req.scope,
                                 overrides=req.overrides)
        # modify pushes only the change-jobs heartbeat (no profile); it uses the
        # executor's IDC_CALLBACK_BASE, so callback is left empty.
        payload: Dict[str, Any] = {
            "app_id": req.app_id, "repo_url": req.repo_url, "branch": req.branch,
            "mode": req.mode, "scope": req.scope or [],
            "changes": spec.changes, "notes": spec.notes, "callback": ""}
        res = _enqueue("modify", payload, req.executor_id)
        # surface the concrete change list + notes back to the operator so they
        # can see exactly what was enqueued for the executor (audit/preview).
        res["changes"] = spec.changes
        res["notes"] = spec.notes
        return res
    # scan / comb push a CodeProfile back to /api/code-profiles/{app_id}.
    cb = ec.cb(f"/api/code-profiles/{req.app_id}")
    payload = {"app_id": req.app_id, "repo_url": req.repo_url,
               "branch": req.branch, "callback": cb}
    return _enqueue(req.action, payload, req.executor_id)


class DBScanReq(BaseModel):
    db_server_id: str          # stable DB host identity (hostname)
    source_engine: str = ""    # oracle / sqlserver / mysql
    target_engine: str = ""   # tdsql / cdb_mysql / postgresql
    mode: Literal["assess", "convert"] = "assess"   # F6 — convert also emits DDL + report
    executor_id: Optional[str] = None


@app.post("/api/db-scan")
def db_scan_trigger(req: DBScanReq):
    """Ask the external executor to run a heterogeneous DB-scan on a DB host (F5).

    The executor scans the DB schema/SQL and pushes a ``DBConversionProfile``
    back to ``PUT /api/db-profiles/{db_server_id}``. Rebuild then folds the
    grade into the host's match (confidence dip / replatform force) + wave risk.
    """
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    payload = {"db_server_id": req.db_server_id, "source_engine": req.source_engine,
               "target_engine": req.target_engine, "mode": req.mode,
               "callback": ec.cb(f"/api/db-profiles/{req.db_server_id}")}
    return _enqueue("db-scan", payload, req.executor_id)


class RuntimeContainerizeReq(BaseModel):
    app_id: str
    server_id: str                 # host whose runtime inventory we infer from
    inventory: Dict[str, Any] = {}  # process / port / software (Zabbix/Prometheus)
    mode: Literal["plan", "execute"] = "plan"   # plan (dry-run) | execute (write)
    executor_id: Optional[str] = None


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
    s = _resolve_executor(req.executor_id)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled (IDC_EXECUTOR_ENABLED=false)")
    ec = get_executor_client(s)
    # auto-gather + merge when the operator didn't supply a full inventory
    from ..core.runtime_inventory import gather_runtime_inventory, merge_inventory
    inventory = dict(req.inventory or {})
    # always normalize operator input (software string -> list) so the executor
    # gets a consistent shape regardless of how the form sent it
    sw = inventory.get("software")
    if isinstance(sw, str):
        inventory["software"] = [x.strip() for x in sw.split(",") if x.strip()]
    # only hit telemetry (Zabbix/Prometheus round-trips) when the operator didn't
    # supply ports — the main runtime signal. Other gaps are filled from match/role.
    srv = None
    if not inventory.get("ports"):
        srv = next((x for x in STORE.list_all_servers() if x.id == req.server_id), None)
        if srv:
            matches = STORE.list_matches()
            m = next((x for x in matches if x.server_id == req.server_id), None)
            profile = STORE.get_code_profile((srv.app_ids or [""])[0]) if srv.app_ids else None
            gathered = gather_runtime_inventory(srv, m, profile=profile, settings=settings)
            inventory = merge_inventory(gathered, inventory)
    payload = {"app_id": req.app_id, "server_id": req.server_id,
               "inventory": inventory, "mode": req.mode,
               "callback": ec.cb(f"/api/code-profiles/{req.app_id}")}
    return _enqueue("runtime-containerize", payload, req.executor_id)


@app.get("/api/executor/jobs/{job_id}")
def executor_job(job_id: str):
    """Look up a dispatched task's status. In pull mode the executor exposes
    nothing, so there is no proxy poll — the task row in the queue is the
    authoritative status (pending/claimed/running/done/error)."""
    t = STORE.get_task(job_id)
    if not t:
        raise HTTPException(404, f"no such task: {job_id}")
    return t


# ---------------------------------------------------------------------------
# Pull dispatch — the executor pulls work from idc-migrate (it exposes nothing
# to the network, so idc-migrate cannot push to it). The executor authenticates
# with its bearer token; the token resolves to an executor id, which both
# identifies the claimer (claimed_by) and gates routing (a task targets one
# executor or the pool). See docs/agent-executor.md §2.2.
# ---------------------------------------------------------------------------
EXECUTOR_LEASE_SECONDS = 900   # 15-min claim lease; expired -> requeue to pending


def _caller_executor_id(authorization: Optional[str]) -> str:
    """Resolve the request's bearer token to an executor id. 401 if the token is
    missing or matches no registered executor."""
    from ..agent import registry
    auth = authorization or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    tok = auth.split(" ", 1)[1].strip()
    eid = registry.resolve_by_token(STORE, settings, tok)
    if not eid:
        raise HTTPException(401, "invalid executor token")
    return eid


class ClaimReq(BaseModel):
    lease_seconds: Optional[int] = None   # override the default claim lease


@app.post("/api/executor/tasks/claim")
def executor_claim_task(req: ClaimReq,
                        authorization: Optional[str] = Header(None)):
    """Executor pulls one eligible task. Eligible = a task targeting this
    executor (by id) or a pool task (executor_id NULL), oldest first. The claim
    is atomic and holds a lease; an expired lease is requeued first (so a dead
    executor's stranded work is reclaimable). Returns the task (with the full
    payload incl the feedback ``callback``) or ``{task_id: null, status:"idle"}``
    when nothing is pending. Bearer auth — the token identifies the executor."""
    eid = _caller_executor_id(authorization)
    s = _executor_settings(eid)
    if not s.executor_enabled:
        raise HTTPException(409, "executor disabled")
    lease = EXECUTOR_LEASE_SECONDS
    if req.lease_seconds is not None:
        if not (10 <= req.lease_seconds <= 86400):
            raise HTTPException(400, "lease_seconds must be between 10 and 86400")
        lease = req.lease_seconds
    # the executor just reached into idc-migrate — record it so the Manage
    # executors panel can show "connected" from inbound activity (idc-migrate
    # never probes). An idle poll (no task to hand over) still counts.
    STORE.touch_executor_seen(eid, _now_iso())
    t = STORE.claim_next_task(eid, lease, _now_iso())
    if not t:
        return {"task_id": None, "status": "idle"}
    return {"task_id": t["id"], "kind": t["kind"], "status": t["status"],
            "payload": t["payload"], "executor_id": t["executor_id"],
            "claimed_at": t["claimed_at"], "lease_until": t["lease_until"]}


class CompleteReq(BaseModel):
    status: Literal["done", "error"]
    error: Optional[str] = None
    result_ref: Optional[str] = None
    summary: Optional[str] = None


@app.post("/api/executor/tasks/{task_id}/complete")
def executor_complete_task(task_id: str, req: CompleteReq,
                           authorization: Optional[str] = Header(None)):
    """Executor marks a claimed task terminal. Only the owning executor (the one
    that claimed it) may complete it; a task whose lease already expired and was
    reclaimed by another executor is a no-op here (returns 409 so the caller
    knows its work was taken over). Bearer auth."""
    eid = _caller_executor_id(authorization)
    STORE.touch_executor_seen(eid, _now_iso())   # inbound activity heartbeat
    ok = STORE.complete_task(task_id, eid, req.status, _now_iso(),
                             error=req.error, result_ref=req.result_ref,
                             summary=req.summary)
    if not ok:
        raise HTTPException(409, f"task {task_id} not owned by this executor "
                                   f"(expired/requeued) — work was reclaimed")
    # mirror the terminal state into the ChangeJob audit trail so the existing
    # /api/change-jobs UI (and any consumer of ChangeJob) still sees the run.
    _mirror_task_change_job(task_id)
    return {"task_id": task_id, "status": req.status}


class LeaseReq(BaseModel):
    lease_seconds: Optional[int] = None


@app.post("/api/executor/tasks/{task_id}/lease")
def executor_renew_lease(task_id: str, req: LeaseReq,
                         authorization: Optional[str] = Header(None)):
    """Executor heartbeat — extend the claim's lease while still working. Only
    the owning executor may renew. Bearer auth."""
    eid = _caller_executor_id(authorization)
    STORE.touch_executor_seen(eid, _now_iso())   # inbound activity heartbeat
    lease = EXECUTOR_LEASE_SECONDS
    if req.lease_seconds is not None:
        if not (10 <= req.lease_seconds <= 86400):
            raise HTTPException(400, "lease_seconds must be between 10 and 86400")
        lease = req.lease_seconds
    ok = STORE.renew_lease(task_id, eid, lease, _now_iso())
    if not ok:
        raise HTTPException(409, f"task {task_id} not owned by this executor "
                                   f"(expired/requeued)")
    return {"task_id": task_id, "status": "claimed",
            "lease_until": _lease_expiry_pub(task_id)}


def _lease_expiry_pub(task_id: str) -> Optional[str]:
    t = STORE.get_task(task_id)
    return t["lease_until"] if t else None


def _mirror_task_change_job(task_id: str) -> None:
    """Best-effort: also record the terminal task state as a ChangeJob so the
    existing change-jobs UI/audit trail (keyed by job id == task id) keeps
    working without every consumer learning the executor_tasks table."""
    try:
        t = STORE.get_task(task_id)
        if not t:
            return
        p = t.get("payload") or {}
        from ..core.models import ChangeJob
        j = ChangeJob(
            id=task_id,
            app_id=p.get("app_id") or p.get("db_server_id") or p.get("server_id")
                or p.get("scope_id") or p.get("wave_id") or "",
            kind=t["kind"], repo_url=p.get("repo_url", ""), branch=p.get("branch", ""),
            status=t["status"], patch_ref=t.get("result_ref") or "",
            summary=t.get("summary") or "", error=t.get("error") or "",
            created_at=t.get("created_at") or "", finished_at=t.get("finished_at") or "")
        STORE.upsert_change_job(j)
    except Exception:
        pass   # audit mirror is best-effort; never fail the complete call


@app.get("/api/executor/tasks")
def list_executor_tasks(status: Optional[str] = None,
                         executor_id: Optional[str] = None,
                         limit: int = 200):
    """List dispatched tasks (newest-first). Operator/UI view of the queue."""
    return STORE.list_tasks(limit=limit, status=status, executor_id=executor_id)


@app.get("/api/executor/tasks/{task_id}")
def get_executor_task(task_id: str):
    t = STORE.get_task(task_id)
    if not t:
        raise HTTPException(404, f"no such task: {task_id}")
    return t


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

    global _REBUILD_RUNNING
    if not _REBUILD_LOCK.acquire(blocking=False):
        raise HTTPException(409, "a rebuild is already running; wait for it to finish")
    _REBUILD_RUNNING = True
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
            _REBUILD_LOCK.release()
            _REBUILD_RUNNING = False

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
def ask(req: AskReq):
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
    return {"server_id": req.server_id, "explanation": LLM.explain_match(s, m, _first_code_profile(s)),
            "rule_rationale": m.rationale}


@app.post("/api/right-size")
def right_size(req: ExplainReq):
    s = STORE.get_server(req.server_id)
    if not s:
        raise HTTPException(404, "server not found")
    m = STORE.get_match(req.server_id)
    if not m:
        raise HTTPException(404, "no match for server")
    return {"server_id": req.server_id, "advice": LLM.right_size(s, m, _first_code_profile(s))}


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
    res = LLM.audit_match(s, m, _first_code_profile(s))
    res["server_id"] = req.server_id
    res["rule_target"] = {"product": m.target.product, "spec": m.target.spec,
                          "confidence": m.confidence}
    return res


@app.post("/api/strategy/host")
def strategy_host(req: ExplainReq):
    """Per-host 7R analysis (advisory). Asks the MigraQ what 7R strategy fits
    THIS host, grounded in its full context (per-partition disks, OS-EOL /
    warranty buckets, utilization, code profile, the rule target, the host's
    current 7R policy + any operator retain/retire override) — the per-host
    sibling of ``POST /api/strategy`` (which is per-app).

    Returns ``{ok, server_id, hostname, current_7r, host_disposition, strategy,
    rationale, target, confidence, effort, key_changes}``. Advisory only —
    does NOT mutate. The operator acts via "set disposition" (retain/retire) or
    the per-app "7R strategy" button (a migrating R affects the whole app)."""
    from ..core.cost import seven_r_for, host_disposition_for
    s = STORE.get_server(req.server_id)
    if not s:
        raise HTTPException(404, "server not found")
    m = STORE.get_match(req.server_id)
    if not m:
        raise HTTPException(404, "no match for server")
    # preload the small strategy + disposition maps once (not per host) to derive
    # the host's CURRENT 7R policy (same source of truth as the Inventory column
    # + Business case) + any operator retain/retire override, both sent to the
    # LLM so it can recommend per-host without re-litigating an operator call.
    smap = {x.app_id: x for x in STORE.list_app_strategies()}
    dk = {d.server_id: d.disposition for d in STORE.list_legacy_dispositions()}
    current = seven_r_for(s, smap, dk.get)
    host_disp = host_disposition_for(s, dk.get)
    res = LLM.seven_r_host(s, m, _first_code_profile(s),
                          current=current, host_disposition=host_disp)
    res["server_id"] = req.server_id
    res["hostname"] = s.hostname
    res["current_7r"] = current
    res["host_disposition"] = host_disp
    return res


class StrategyHostBatchReq(BaseModel):
    apply: bool = False
    batch_size: int = 50      # hosts per batch — committed before the next batch starts
    cursor: Optional[str] = None   # resume: hostname to start AFTER (None = from start)
    limit: int = 0            # max hosts THIS call; 0 = one batch (batch_size hosts)


@app.post("/api/strategy/host/batch")
def strategy_host_batch(req: StrategyHostBatchReq,
                        role: Optional[str] = None, env: Optional[str] = None,
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
                        app_id: Optional[str] = None):
    """Bulk per-host 7R analysis over the WHOLE filter, in batches, committed
    per batch + resumable. NDJSON-streamed.

    Processes every host matching the current inventory filter (the SAME filter
    params as GET /api/servers) via the MigraQ ``seven_r`` skill, in ordered
    batches of ``batch_size`` hosts (by hostname asc). The client drives the
    loop: each call processes one batch (``limit=0``) or up to ``limit`` hosts,
    commits that batch's mutations, and returns a ``cursor`` (the next hostname)
    + ``more=true`` when hosts remain — the UI calls again with the cursor to
    continue. This keeps each HTTP request short (~batch_size × 3s) so a long
    full-estate run (13,855 hosts × ~3s ≈ 11h) survives a network blip, a backend
    restart, or a browser-tab close: the committed batches are durable, and the
    run resumes from the cursor instead of restarting from zero.

    ``apply``: when true, changes each host's 7R policy to match the
    recommendation —
      * retain/retire -> ``upsert_legacy_disposition`` (the host-level override
        the Inventory 7R column + Business case + wave planner all read);
      * migrating R  -> ``delete_legacy_disposition`` (clear any retain/retire
        override so the host migrates with its app) + tally per app, and a
        migrating recommendation fills the app's strategy ONLY when the app has
        no existing strategy (operator/AI strategies preserved — non-destructive).
    Each host's mutation is committed immediately (autocommit), and the
    per-batch app-strategy fill commits with the batch — so a batch is fully
    durable before the next begins. When ``apply`` is false, dry-run preview
    only (no mutation). The LLM sees each host's current 7R + any operator
    retain/retire override, so it respects an explicit operator call.

    ``cursor``: resume point (hostname to start AFTER, by asc order). The UI
    passes the previous call's returned cursor back. ``batch_size``: hosts per
    batch (default 50). ``limit``: max hosts this call (0 = one batch).

    Uses the SAME ``seven_r`` skill + ``seven_r_for``/``host_disposition_for``
    precedence as the per-host Analyze-7R drawer button + the Inventory 7R
    column, so the bulk result is consistent with everything else.
    """
    from ..core.models import AppStrategy, STRATEGY_SOURCE_AI, _now
    from ..core.cost import seven_r_for, host_disposition_for
    from ..core.codeintel import pattern_rank

    f = ServerFilter(role=role, env=env, status=status, source_type=source_type,
                     os=os, criticality=criticality, cluster=cluster, datacenter=datacenter,
                     target_product=target_product, wave_id=wave_id, q=q,
                     util_cpu_min=util_cpu_min, util_mem_min=util_mem_min,
                     util_disk_min=util_disk_min, conf_min=conf_min, conf_max=conf_max,
                     warranty_bucket=warranty_bucket, os_eol_bucket=os_eol_bucket,
                     app_id=app_id, hostname_after=req.cursor)
    batch_size = max(1, min(int(req.batch_size or 50), 500))
    # hosts to process this call: one batch when limit<=0, else up to limit
    hosts_target = min(batch_size, 100000) if req.limit <= 0 else max(batch_size, min(int(req.limit), 100000))

    def stream():
        # preload everything ONCE per call — the small maps + the matches + the
        # code profiles, so each host is an O(1) lookup + one LLM call. The cursor
        # (hostname > ?) is in the filter, so query_servers resumes efficiently.
        page = STORE.query_servers(f, page=1, page_size=hosts_target + 1,
                                    order_by="hostname", order_dir="asc",
                                    with_facets=False)
        rows = page["items"]
        total_matching = page["total"]
        more = len(rows) > hosts_target
        servers = rows[:hosts_target]   # drop the +1 probe row
        matches = {m.server_id: m for m in STORE.list_matches()}
        smap = {x.app_id: x for x in STORE.list_app_strategies()}
        dk = {d.server_id: d.disposition for d in STORE.list_legacy_dispositions()}
        profiles_by_app = {p.app_id: p for p in STORE.list_code_profiles()}

        yield json.dumps({"type": "start", "total": total_matching,
                          "batch_size": batch_size, "apply": req.apply,
                          "cursor": req.cursor, "resumed": req.cursor is not None,
                          "this_call": len(servers), "more": more},
                         ensure_ascii=False) + "\n"

        analyzed = errors = applied_host = applied_clear = applied_app = 0
        batches = 0
        last_hostname = req.cursor

        existing_apps = {x.app_id for x in STORE.list_app_strategies()}

        batch_tally: Dict[str, List[str]] = {}
        batch_host_n = 0
        batch_idx = 0

        for i, s in enumerate(servers, 1):
            last_hostname = s.hostname
            m = matches.get(s.id)
            if not m:
                yield json.dumps({"type": "host", "server_id": s.id,
                                  "hostname": s.hostname, "ok": False,
                                  "note": "no match — skipped", "done": i},
                                 ensure_ascii=False) + "\n"
                batch_host_n += 1
            else:
                current = seven_r_for(s, smap, dk.get)
                host_disp = host_disposition_for(s, dk.get)
                prof = next((profiles_by_app[a] for a in (s.app_ids or [])
                            if a in profiles_by_app), None)
                try:
                    rec = LLM.seven_r_host(s, m, prof, current=current,
                                          host_disposition=host_disp)
                except Exception as e:   # never let one host kill the stream
                    errors += 1
                    yield json.dumps({"type": "host", "server_id": s.id,
                                      "hostname": s.hostname, "ok": False,
                                      "error": f"{e!r}", "done": i},
                                     ensure_ascii=False) + "\n"
                    batch_host_n += 1
                else:
                    analyzed += 1
                    applied = "none"
                    if req.apply and rec.get("ok"):
                        keys = _host_disposition_keys(s)
                        key = keys[0] if keys else s.id
                        strat = rec["strategy"]
                        if strat in (LD_RETAIN, LD_RETIRE):
                            # committed immediately (autocommit) — durable per host
                            STORE.upsert_legacy_disposition(LegacyDisposition(
                                server_id=key, disposition=strat,
                                rationale=(rec.get("rationale") or "AI 7R batch").strip(),
                                confidence=float(rec.get("confidence") or 0.5),
                                summary=f"AI 7R batch ({strat})"))
                            dk[key] = strat    # keep the in-mem map fresh
                            applied_host += 1
                            applied = strat
                        else:
                            if host_disp:
                                STORE.delete_legacy_disposition(key)
                                dk.pop(key, None)
                                applied_clear += 1
                                applied = "cleared"
                            for a in (s.app_ids or []):
                                batch_tally.setdefault(a, []).append(strat)
                    yield json.dumps({"type": "host", "server_id": s.id,
                                      "hostname": s.hostname,
                                      "app_ids": list(s.app_ids or []),
                                      "current_7r": current,
                                      "host_disposition": host_disp,
                                      "ok": rec.get("ok", False),
                                      "strategy": rec.get("strategy", ""),
                                      "rationale": rec.get("rationale", ""),
                                      "confidence": rec.get("confidence"),
                                      "target": rec.get("target", ""),
                                      "applied": applied, "done": i},
                                     ensure_ascii=False) + "\n"
                    batch_host_n += 1

            # batch boundary: every batch_size hosts, OR the last host of the call
            is_last_of_call = (i == len(servers))
            if batch_host_n >= batch_size or is_last_of_call:
                # commit the batch's app-strategy fills (non-destructive) — this is
                # the "commit after each batch" checkpoint: every host disposition
                # is already autocommitted; the app fills commit here, then the
                # batch frame marks the durable boundary before the next batch.
                if req.apply and batch_tally:
                    for app_id, recs in batch_tally.items():
                        if app_id in existing_apps:
                            continue
                        counts: Dict[str, int] = {}
                        for r in recs:
                            counts[r] = counts.get(r, 0) + 1
                        top = sorted(counts.items(),
                                     key=lambda kv: (-kv[1],
                                                     pattern_rank(CodeProfile(
                                                         app_id=app_id,
                                                         migration_pattern=kv[0]))))
                        strat = top[0][0]
                        STORE.upsert_app_strategy(AppStrategy(
                            app_id=app_id, strategy=strat,
                            rationale=f"AI 7R batch majority of {len(recs)} host recs",
                            source=STRATEGY_SOURCE_AI, confidence=0.6,
                            assigned_at=_now(), updated_at=_now()))
                        existing_apps.add(app_id)
                        applied_app += 1
                        yield json.dumps({"type": "app", "app_id": app_id,
                                          "strategy": strat, "host_count": len(recs),
                                          "applied": True}, ensure_ascii=False) + "\n"
                    batch_tally = {}
                batches += 1
                batch_idx += 1
                batch_host_n = 0
                # cursor = last processed hostname so the next call resumes AFTER it
                next_cursor = last_hostname if more else None
                yield json.dumps({"type": "batch", "index": batch_idx,
                                  "committed": True, "more": more,
                                  "cursor": next_cursor,
                                  "total_done": i, "total": total_matching},
                                 ensure_ascii=False) + "\n"

        yield json.dumps({"type": "done", "total": total_matching,
                          "analyzed": analyzed, "errors": errors,
                          "applied_host": applied_host, "applied_clear": applied_clear,
                          "applied_app": applied_app, "batches": batches,
                          "more": more, "cursor": (last_hostname if more else None),
                          "apply": req.apply}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


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
    app_strategies + F2 tag-driven retain/retire) + the per-host disposition
    overrides (retain/retire set via the Inventory drawer) for portfolio costing.

    The host_dispositions map is what makes the Business case's 7R policy match
    the Inventory's: a host the operator set to retain/retire in the drawer
    (legacy_dispositions table) is shown with that same 7R outcome here, instead
    of its app-level AI strategy."""
    from ..core.cost import strategies_from_tags, host_disposition_for
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    strategies = {s.app_id: s for s in STORE.list_app_strategies()}
    # operator tag-driven retain/retire wins over the AI strategy
    for app_id, strat in strategies_from_tags(servers).items():
        strategies[app_id] = strat
    # per-host retain/retire override (Inventory drawer "set disposition") —
    # keyed by server id, applied on top of the app-level strategy in
    # estimate_portfolio so the Business case shows the same 7R as the drawer.
    # Preload the (small) legacy_dispositions table ONCE and probe by stable
    # key, not a DB round-trip per host (13,855 hosts would otherwise be 13,855
    # round-trips). Same keys + filter as the Inventory list + drawer.
    dk = {d.server_id: d.disposition for d in STORE.list_legacy_dispositions()}
    host_dispositions = {s.id: hd for s in servers if (hd := host_disposition_for(s, dk.get))}
    return servers, matches, strategies, host_dispositions


@app.post("/api/business-case")
def business_case(req: BusinessCaseReq):
    """Compute the portfolio TCO + per-strategy rollup (F2). Optionally save
    an immutable snapshot to ``business_case_snapshots``."""
    from ..core.cost import estimate_portfolio, load_pricebook
    from ..core.models import _new_id
    servers, matches, strategies, host_dispositions = _portfolio_inputs()
    book = load_pricebook(settings)
    payload = estimate_portfolio(servers, matches, book, strategies=strategies,
                                 host_dispositions=host_dispositions)
    if req.save:
        sid = _new_id("bc")
        STORE.save_business_case(sid, payload)
        payload["snapshot_id"] = sid
    return payload


def _business_case_stale(payload: Dict[str, Any]) -> bool:
    """True when a saved business-case snapshot no longer describes the current
    estate, so serving it would show data from a *previous* estate (e.g. the
    old demo/scale fixture's ``db-``/``app-``/``web-`` hosts after the real
    ``inventory_draft`` was loaded, any snapshot left over from before a
    Rebuild, OR a snapshot whose per-server 7R strategies predate a 7R-policy
    change). A snapshot's ``per_server`` list carries one row per matched
    server, so its length is the matched-server count at save time; compare it
    to the live ``matches`` count. A mismatch means the estate changed
    underneath the snapshot and it must be recomputed before display.

    The 7R policy (the Inventory column + Business case row) is host_disposition
    > app_strategies > rehost; a snapshot saved before an app-strategy /
    host-disposition change would otherwise show stale 7R values that differ
    from the live Inventory 7R column even though the matched-server count is
    unchanged. So also treat the snapshot as stale when any app_strategies /
    legacy_dispositions row was updated AFTER the snapshot's ``generated_at``."""
    if not isinstance(payload, dict):
        return True
    # one per_server row per matched server -> its length is the matched-server
    # count at save time; the live matches count is its current counterpart.
    snap_n = len(payload.get("per_server") or [])
    live_n = int(STORE.stats().get("matches") or 0)
    if snap_n != live_n:
        return True
    # priced+unpriced is the same matched-server count, tallied at save time;
    # double-check it as a guard against a hand-edited/corrupt payload.
    tally = int(payload.get("priced_servers") or 0) + int(payload.get("unpriced_servers") or 0)
    if tally and tally != live_n:
        return True
    # 7R-policy drift: a strategy / disposition written after the snapshot was
    # generated means the snapshot's per-server strategies no longer match the
    # live Inventory 7R column -> recompute. ``generated_at`` is set at compute
    # time; compare it to the newest updated_at across the two small tables.
    gen = payload.get("generated_at") or ""
    if gen:
        newest = ""
        for upd in (s.updated_at for s in STORE.list_app_strategies() if s.updated_at):
            if upd > newest:
                newest = upd
        for upd in (d.updated_at for d in STORE.list_legacy_dispositions() if d.updated_at):
            if upd > newest:
                newest = upd
        if newest and newest > gen:
            return True
    return False


@app.get("/api/business-case")
def latest_business_case():
    """Return the most recently saved business-case snapshot, or 404.

    A snapshot is immutable, so once the estate changes (new inventory load /
    Rebuild) it goes stale and would display a *previous* estate's per-server
    list. Detect that by comparing the snapshot's matched-server count to the
    live ``matches`` count and, when stale, recompute from the current estate
    in place (without persisting — the Refresh button saves a fresh snapshot).
    This keeps the UI from ever serving a stale demo-fixture snapshot."""
    snaps = STORE.list_business_cases(limit=1)
    if not snaps:
        raise HTTPException(404, "no saved business case; POST /api/business-case first")
    snap = STORE.get_business_case(snaps[0]["id"])
    payload = snap["payload"]
    if _business_case_stale(payload):
        from ..core.cost import estimate_portfolio, load_pricebook
        servers, matches, strategies, host_dispositions = _portfolio_inputs()
        book = load_pricebook(settings)
        payload = estimate_portfolio(servers, matches, book, strategies=strategies,
                                     host_dispositions=host_dispositions)
        # keep the saved snapshot's id for reference, and flag that this response
        # was recomputed live (not a saved snapshot) so the UI can hint a save.
        payload["snapshot_id"] = snap["id"]
        payload["recomputed"] = True
        snap = {"id": snap["id"], "created_at": snap["created_at"], "payload": payload}
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
    servers, matches, strategies, host_dispositions = _portfolio_inputs()
    book = load_pricebook(settings)
    return what_if(servers, matches, book, region=req.region,
                   sizing=req.sizing, byol=req.byol, strategies=strategies,
                   host_dispositions=host_dispositions)


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
def launch_wave_exec(wave_id: str, kind: Literal["host", "db", "code"] = "host"):
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
    ok, _ = run_validation_gates(j, settings, store=STORE)
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
    policy) + per-server classification over the current estate (F8).

    The archetype SET + blueprints come from the active LZ design — the
    operator's authored design if one is active, else the built-in
    ``LZ_BLUEPRINTS`` default. The web forwards ``archetypes[<arch>]`` as the
    ``context`` to ``POST /api/iac-emit``, so emitting IaC for an archetype
    always uses the active design's blueprint (Phase A)."""
    from ..core.lz import (active_lz_archetypes, archetypes_for_servers,
                           resolve_all_archetype_blueprints, get_active_lz_design,
                           active_classifier)
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    archetypes = active_lz_archetypes(STORE)
    blueprints = resolve_all_archetype_blueprints(STORE)
    classifier = active_classifier(STORE)
    cls = archetypes_for_servers(servers, matches, classifier)
    # the active network design's placements (keyed by stable identity_key) so
    # the per-server rows can carry their VPC/subnet/IP without the UI having to
    # join by hostname (which would miss fqdn-only servers — identity_key is
    # fqdn-or-hostname).
    nd = STORE.get_active_network_design()
    placements = (nd.placements or {}) if nd else {}
    arch_overrides = (nd.archetype_overrides or {}) if nd else {}
    counts = {a: 0 for a in archetypes}
    per_server = []
    for s in servers:
        key = s.identity_key or f"id:{s.id}"
        # a VPC pin (archetype override) reclassifies the server for placement —
        # overlay it here so the Estate classification archetype column agrees
        # with the VPC column (which comes from the placement below).
        a = arch_overrides.get(key) or cls.get(s.id, archetypes[0] if archetypes else "corp")
        counts[a] = counts.get(a, 0) + 1
        per_server.append({"server_id": s.id, "hostname": s.hostname,
                           "identity_key": key, "role": s.role, "archetype": a,
                           "app_ids": list(s.app_ids or []),
                           "placement": placements.get(key)})
    design = get_active_lz_design(STORE)
    return {"archetypes": {a: blueprints[a] for a in archetypes},
            "counts": counts, "per_server": per_server,
            "design": {"design_id": design.design_id, "name": design.name,
                       "is_active": design.is_active,
                       "is_builtin": design.design_id.startswith("builtin"),
                       "onprem_cidrs": design.onprem_cidrs,
                       "has_classifier": classifier is not None},
            "network_active": nd is not None}


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
    workload waves targeting that archetype (the placement gate). The
    archetype must exist in the active LZ design (built-in corp/online/dmz or
    an operator-authored design's archetype set)."""
    from ..core.lz import LZ_STATUSES, active_lz_archetypes
    if req.status not in LZ_STATUSES:
        raise HTTPException(400, f"status must be one of {LZ_STATUSES}")
    if archetype not in active_lz_archetypes(STORE):
        raise HTTPException(400, f"archetype {archetype!r} not in the active LZ design")
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
# F8 (Phase A) — operator-authored LZ designs (LLM-driven LZ design foundation)
# ---------------------------------------------------------------------------
@app.get("/api/lz/designs")
def lz_designs_list():
    """List all LZ designs (summary) + which one is active. Phase A lets an
    operator persist + activate a hand-authored design; Phase B adds the LLM
    interview that PRODUCES the design. The active design drives
    /api/lz/archetypes, lz_context, iac-emit, lz_readiness, check_lz_gate."""
    from ..core.lz import get_active_lz_design
    out = []
    active = get_active_lz_design(STORE)
    for d in STORE.list_lz_designs():
        out.append({"design_id": d.design_id, "name": d.name,
                    "summary": d.summary or "",
                    "is_active": d.is_active,
                    "archetype_count": len(d.archetypes or {}),
                    "archetypes": list((d.archetypes or {}).keys()),
                    "scale": d.scale or "",
                    "updated_at": d.updated_at, "updated_by": d.updated_by})
    return {"designs": out, "active": {"design_id": active.design_id,
            "name": active.name, "is_builtin": active.design_id.startswith("builtin"),
            "scale": getattr(active, "scale", "") or ""}}


@app.get("/api/lz/designs/{design_id}")
def lz_designs_get(design_id: str):
    d = STORE.get_lz_design(design_id)
    if not d:
        raise HTTPException(404, "design not found")
    return d.to_dict()


@app.post("/api/lz/designs")
def lz_designs_create(body: dict):
    """Create (or update if ``design_id`` present) an LZ design. Validates the
    blueprint deterministically (CIDR non-overlap, peering symmetry, required
    keys) — a design that fails validation is rejected with 422 + the error
    list so the LLM (Phase B) or the operator can fix it. Does NOT activate;
    call /api/lz/designs/{id}/activate to make it drive the gate."""
    from ..core.lz import validate_lz_design
    from ..core.models import LZDesign, _new_id, _now
    design_id = (body.get("design_id") or "").strip() or _new_id("lzd")
    d = LZDesign(
        design_id=design_id,
        name=str(body.get("name") or "").strip() or f"design-{design_id[-4:]}",
        summary=str(body.get("summary") or "").strip()[:500],
        archetypes=body.get("archetypes") or {},
        requirements=body.get("requirements") or {},
        conversation=body.get("conversation") or [],
        onprem_cidrs=body.get("onprem_cidrs") or [],
        is_active=False,
        scale=str(body.get("scale") or ""),
        created_at=(STORE.get_lz_design(design_id) or LZDesign()).created_at or _now(),
        updated_at=_now(),
        updated_by=str(body.get("updated_by") or "operator"),
    )
    v = validate_lz_design(d)
    if not v["ok"]:
        raise HTTPException(422, f"invalid LZ design: {'; '.join(v['errors'])}")
    STORE.upsert_lz_design(d)
    return {"design_id": d.design_id, "name": d.name, "valid": True,
            "is_active": False, "archetypes": list(d.archetypes.keys())}


@app.post("/api/lz/designs/{design_id}/activate")
def lz_designs_activate(design_id: str, body: Optional[dict] = None):
    """Make ``design_id`` the sole active LZ design (atomic single-active
    invariant). The design must already exist + pass validation."""
    from ..core.lz import validate_lz_design
    d = STORE.get_lz_design(design_id)
    if not d:
        raise HTTPException(404, "design not found")
    v = validate_lz_design(d)
    if not v["ok"]:
        raise HTTPException(422, f"cannot activate invalid design: {'; '.join(v['errors'])}")
    by = str((body or {}).get("by") or "operator")
    STORE.set_active_lz_design(design_id, updated_by=by)
    return {"design_id": design_id, "is_active": True, "updated": True}


@app.delete("/api/lz/designs/{design_id}")
def lz_designs_delete(design_id: str):
    d = STORE.get_lz_design(design_id)
    if not d:
        raise HTTPException(404, "design not found")
    if d.is_active:
        raise HTTPException(409, "deactivate the design before deleting (or "
                            "activate another); an active design can't be deleted")
    STORE.delete_lz_design(design_id)
    return {"design_id": design_id, "deleted": True}


class LZInterviewReq(BaseModel):
    demand: str
    onprem_cidrs: Optional[List[str]] = None
    design_id: Optional[str] = None      # continue an existing design's interview
    conversation: Optional[List[Dict[str, str]]] = None  # or pass turns inline
    scale: Optional[str] = None          # small/medium/large override (else inferred)
    by: str = "operator"


@app.get("/api/lz/scale-tiers")
def lz_scale_tiers():
    """The 3 LZ scale tiers (small/medium/large) with their default-strategy
    comparison matrix (account/networking/security/audit/budgeting/identity +
    topology + archetype count) and the cross-scale ``LZ_GOLDEN_RULES``. The UI
    renders this as the comparison matrix + the golden-rule banner; the operator
    picks a tier (or accepts the inferred one) and the rest is default strategy,
    while the user-modifiable parts flow through the AI prompt interview."""
    from ..core.lz import LZ_SCALE_TIERS, LZ_SCALE_TIERS_ORDER, LZ_GOLDEN_RULES
    return {"tiers": [LZ_SCALE_TIERS[s] for s in LZ_SCALE_TIERS_ORDER],
            "order": list(LZ_SCALE_TIERS_ORDER),
            "golden_rules": LZ_GOLDEN_RULES}


@app.get("/api/lz/scale")
def lz_scale_inferred():
    """The scale tier inferred from the live estate (``lz.scale_for``) — what
    the UI defaults the scale picker to. Returns the full estate sizing metrics
    (servers, apps, total vCPU cores, total memory GB, baremetal count, regulated
    flag, by-env distribution) so the operator can compare their real estate
    against the tier's ``estate_size`` bands and sanity-check the inference."""
    from ..core.lz import scale_for, estate_scale_metrics, LZ_SCALE_TIERS
    servers = STORE.list_all_servers()
    m = estate_scale_metrics(servers)
    tier = scale_for(servers, metrics=m)
    return {"scale": tier, "label": LZ_SCALE_TIERS[tier]["label"],
            "signals": m,
            "bands": LZ_SCALE_TIERS[tier]["estate_size"]}


@app.post("/api/lz/design/interview")
def lz_design_interview(req: LZInterviewReq):
    """LLM LZ design interview (Phase B). Drives ``LLMClient.design_lz`` — the
    LLM proposes a blueprint dict and the deterministic ``validate_lz_design``
    loops it until sound (CIDR non-overlap, symmetric peering, required keys).
    ``scale`` (small/medium/large) sets the default strategy the LLM honors
    (inferred from the estate when unset); the operator's ``demand`` customizes
    the user-modifiable parts (app placement, CIDRs, names, compliance tiers).
    Returns the proposed design (NOT persisted) + the validation + the
    conversation; the UI previews it, then POSTs it to /api/lz/designs to persist
    and /api/lz/designs/{id}/activate to make it drive the gate. Pass
    ``design_id`` to continue an existing design's interview (iterate): its
    stored conversation + onprem_cidrs are loaded as the prior turns."""
    demand = (req.demand or "").strip()
    if not demand:
        raise HTTPException(400, "demand is required")
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    conv = list(req.conversation or [])
    onprem = list(req.onprem_cidrs or [])
    scale = (req.scale or "").strip() or None
    if req.design_id:
        d = STORE.get_lz_design(req.design_id)
        if d is None:
            raise HTTPException(404, "design not found")
        if not conv:
            conv = list(d.conversation or [])
        if not onprem:
            onprem = list(d.onprem_cidrs or [])
        # continue at the stored design's scale unless the operator re-pinned
        if not scale and d.scale:
            scale = d.scale
    res = LLM.design_lz(demand, servers, matches, onprem_cidrs=onprem,
                       conversation=conv, scale=scale)
    design = res.get("design")
    if design is not None and req.design_id:
        # keep the same row when iterating so the UI upserts into it
        design.design_id = req.design_id
    return {
        "ok": res["ok"],
        "error": res.get("error") or "",
        "rounds": res.get("rounds") or [],
        "validation": res.get("validation"),
        "design": design.to_dict() if design is not None else None,
    }


# ---------------------------------------------------------------------------
# F8 (Phase B+) — network designer: VPC + subnets per account + placement
# ---------------------------------------------------------------------------
class NetworkDesignReq(BaseModel):
    demand: Optional[str] = None        # if set, ask MigraQ to propose the policy
    policy: Optional[Dict[str, Any]] = None   # explicit policy (else default)
    design_id: Optional[str] = None     # upsert into this row when activating
    activate: bool = False              # persist + make active
    by: str = "operator"


@app.post("/api/lz/network/design")
def network_design(req: NetworkDesignReq):
    """Run the network designer: carve the active LZ design's per-archetype VPCs
    into subnets (one per tier) and place every server into a VPC + subnet + IP.
    With ``demand``, MigraQ proposes the carving policy (LLM propose→validate→
    loop); without it the deterministic ``default_network_policy`` is used.
    Returns the built design + validation. When ``activate`` is set the design is
    persisted (upsert into ``design_id`` or a new row) and made the active one;
    otherwise it's an ephemeral preview (placement overrides need an active
    design — call with activate first)."""
    from ..core.lz import get_active_lz_design
    from ..core.network import build_network_design, default_network_policy
    from ..core.models import _new_id
    lz = get_active_lz_design(STORE)
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    policy = req.policy
    rounds = []
    if req.demand and req.demand.strip():
        pres = LLM.design_network_policy(req.demand.strip(), servers, lz, matches)
        rounds = pres.get("rounds") or []
        if pres["ok"]:
            policy = pres["policy"]            # use the LLM's validated policy
        else:
            # the LLM failed to produce a valid policy — fall back to the
            # deterministic default instead of building with the invalid
            # last-attempted policy (which would yield empty subnets / a broken
            # design). The rounds are still returned so the UI shows why.
            policy = default_network_policy()
    design_id = req.design_id or _new_id("nd")
    existing = STORE.get_network_design(design_id) if req.design_id else None
    nd = build_network_design(lz, servers, matches, policy=policy,
                              design_id=design_id,
                              name=(existing.name if existing else "network design"))
    if req.activate:
        nd.updated_by = req.by
        STORE.upsert_network_design(nd)
        STORE.set_active_network_design(nd.design_id, updated_by=req.by)
    return {"ok": nd.validation.get("ok", False),
            "validation": nd.validation, "rounds": rounds,
            "design": nd.to_dict(), "active": req.activate}


class DistributeReq(BaseModel):
    demand: str
    scale: Optional[str] = None
    onprem_cidrs: Optional[List[str]] = None
    activate: bool = True
    by: str = "operator"


@app.post("/api/lz/distribute")
def lz_distribute(req: DistributeReq):
    """One combined prompt → distribute machines + DBs into VPCs AND subnets in a
    single action. Chains the two LLM steps the LZ tab otherwise runs separately:

      1. LZ design (``LLMClient.design_lz``) — the VPC/ACCOUNT assignment: which
         apps/roles land in which archetype (each a VPC + account), via
         ``requirements.classifier`` (app_map/role_map). Activated so it drives
         the gate and is the basis for step 2.
      2. Network designer (``design_network_policy`` + ``build_network_design``)
         — the SUBNET carving inside each VPC: tier_order + tiers (role→tier
         subnet), built ON TOP of the just-active LZ design (which owns account
         naming — each archetype IS one account). Activated so placements drive
         the Estate classification + topology.

    The SAME ``demand`` is sent to both LLM calls — each extracts the part its
    schema owns (LZ pulls app_map/role_map/archetypes + account naming; network
    pulls tier_order/tiers). ``scale`` sets the LZ default strategy.
    ``activate=False`` = preview (neither design persisted). Returns both
    designs + validation + whether each was activated.
    """
    from ..core.lz import get_active_lz_design
    from ..core.network import build_network_design, default_network_policy
    from ..core.models import _new_id, _now
    demand = (req.demand or "").strip()
    if not demand:
        raise HTTPException(400, "demand is required")
    servers = STORE.list_all_servers()
    matches = STORE.list_matches()
    onprem = list(req.onprem_cidrs or [])
    scale = (req.scale or "").strip() or None

    # Step 1 — LZ design (VPC/account assignment via the classifier).
    lz_res = LLM.design_lz(demand, servers, matches, onprem_cidrs=onprem, scale=scale)
    lz_design = lz_res.get("design")
    lz_activated = False
    if (req.activate and lz_res["ok"] and lz_design and lz_design.archetypes
            and not lz_design.design_id.startswith("builtin")):
        lz_design.updated_by = req.by
        lz_design.updated_at = _now()
        STORE.upsert_lz_design(lz_design)
        STORE.set_active_lz_design(lz_design.design_id, updated_by=req.by)
        lz_activated = True

    # The active LZ step 2 builds on: the just-activated one, else the
    # pre-existing active, else the built-in default (step 2 still runs).
    active_lz = lz_design if lz_activated else get_active_lz_design(STORE)

    # Step 2 — network designer (subnet carving within each VPC). Same demand.
    net_res = LLM.design_network_policy(demand, servers, active_lz, matches)
    net_policy = (net_res.get("policy") if net_res["ok"]
                  else default_network_policy())
    nd = build_network_design(active_lz, servers, matches, policy=net_policy,
                              design_id=_new_id("nd"), name="distributed network")
    net_activated = False
    if req.activate and (nd.validation or {}).get("ok"):
        nd.updated_by = req.by
        STORE.upsert_network_design(nd)
        STORE.set_active_network_design(nd.design_id, updated_by=req.by)
        net_activated = True

    return {
        "lz": {"ok": lz_res["ok"], "error": lz_res.get("error") or "",
               "validation": lz_res.get("validation"),
               "rounds": lz_res.get("rounds") or [],
               "activated": lz_activated,
               "design": lz_design.to_dict() if lz_design else None},
        "network": {"ok": (nd.validation or {}).get("ok", False),
                    "error": net_res.get("error") or "",
                    "validation": nd.validation,
                    "rounds": net_res.get("rounds") or [],
                    "activated": net_activated,
                    "design": nd.to_dict()},
        "active_lz_archetypes": list((active_lz.archetypes or {}).keys()),
    }


@app.get("/api/lz/network/design")
def network_design_active():
    """The active network design (or null when none). Drives the topology +
    placement preview in the web panel."""
    nd = STORE.get_active_network_design()
    return {"active": nd.to_dict() if nd else None}


@app.get("/api/lz/network/designs")
def network_designs_list():
    out = []
    for nd in STORE.list_network_designs():
        out.append({"design_id": nd.design_id, "name": nd.name,
                    "summary": nd.summary or "",
                    "is_active": nd.is_active,
                    "vpc_count": len(nd.vpcs or []),
                    "placement_count": len(nd.placements or {}),
                    "valid": (nd.validation or {}).get("ok", False),
                    "updated_at": nd.updated_at})
    active = STORE.get_active_network_design()
    return {"designs": out, "active": {"design_id": active.design_id,
            "name": active.name} if active else None}


@app.post("/api/lz/network/designs/{design_id}/activate")
def network_design_activate(design_id: str, body: Optional[dict] = None):
    nd = STORE.get_network_design(design_id)
    if not nd:
        raise HTTPException(404, "network design not found")
    if not (nd.validation or {}).get("ok"):
        raise HTTPException(422, "cannot activate an invalid network design: "
                            + "; ".join((nd.validation or {}).get("errors", [])))
    by = str((body or {}).get("by") or "operator")
    STORE.set_active_network_design(design_id, updated_by=by)
    return {"design_id": design_id, "is_active": True, "updated": True}


@app.delete("/api/lz/network/designs/{design_id}")
def network_design_delete(design_id: str):
    nd = STORE.get_network_design(design_id)
    if not nd:
        raise HTTPException(404, "network design not found")
    if nd.is_active:
        raise HTTPException(409, "deactivate the design before deleting")
    STORE.delete_network_design(design_id)
    return {"design_id": design_id, "deleted": True}


class NetworkPlacementReq(BaseModel):
    hostname: str                        # stable identity (lowercased)
    subnet: str = ""                     # target subnet name; "" = clear override
    archetype: str = ""                  # target VPC's archetype; "" = clear override
    account: str = ""                    # target account; "" = clear override
    by: str = "operator"


@app.post("/api/lz/network/placement")
def network_placement_override(req: NetworkPlacementReq):
    """Move one server to a different subnet within its VPC (operator pin,
    survives regenerate) and/or to a different VPC (an archetype pin — VPC is
    1:1 with archetype) and/or to a different account (an account pin — account
    is otherwise derived 1:1 from the archetype, so this reattributes the
    server WITHOUT moving its VPC). Requires an active network design. Re-runs
    the deterministic placement with the updated overrides and persists,
    returning the server's new placement + the design's validation.

    VPC change: ``archetype`` names one of the active design's archetypes (each
    is one VPC). Changing VPC clears any stale subnet pin (subnet names differ
    across VPCs) unless ``subnet`` also names a subnet valid in the NEW VPC.
    Account change: ``account`` names one of the accounts already in the design
    (the union of ``accounts.values()`` / ``vpcs[*].account``). It is independent
    of the VPC — the server keeps its VPC/subnet/IP and only its account
    attribution changes. Send ``account=""`` (and no other field) to revert to the
    VPC's account."""
    from ..core.network import build_network_design
    nd = STORE.get_active_network_design()
    if not nd:
        raise HTTPException(409, "no active network design — generate + apply one first")
    host = (req.hostname or "").strip().lower()
    if not host:
        raise HTTPException(400, "hostname is required")
    servers = STORE.list_all_servers()
    srv = next((s for s in servers if s.identity_key == host), None)
    if srv is None:
        raise HTTPException(404, f"no server with hostname {host!r}")
    from ..core.lz import _classifier_from_design, archetype_for, get_active_lz_design
    lz = get_active_lz_design(STORE)
    clf = _classifier_from_design(lz)
    base_arch = archetype_for(srv, None, clf)
    vpc_by_arch = {v.get("archetype"): v for v in (nd.vpcs or [])}
    # resolve the EFFECTIVE archetype: a VPC pin overrides the classifier result;
    # without one the classifier decides. This is the archetype whose VPC the
    # subnet pin (if any) must belong to.
    arch_overrides = dict(nd.archetype_overrides or {})
    if req.archetype:
        # VPC change — validate the target archetype has a VPC in this design.
        if req.archetype not in vpc_by_arch:
            raise HTTPException(400, f"archetype {req.archetype!r} has no VPC in the "
                                f"active design (have {sorted(vpc_by_arch)})")
        arch_overrides[host] = req.archetype
        arch = req.archetype
    elif host in arch_overrides:
        arch = arch_overrides[host]     # keep the existing VPC pin
    else:
        arch = base_arch
    vpc = vpc_by_arch.get(arch)
    if not vpc:
        raise HTTPException(400, f"server {host!r} archetype {arch!r} has no VPC")
    sn_names = {s.get("name") for s in (vpc.get("subnets") or [])}
    # the set of accounts already in the design (the legal target set for an
    # account pin). account is otherwise derived 1:1 from the archetype, so an
    # account pin reattributes the server to a different existing account.
    known_accounts = sorted({a for a in (nd.accounts or {}).values()}
                            | {v.get("account") for v in (nd.vpcs or [])
                               if v.get("account")})
    account_overrides = dict(nd.account_overrides or {})
    if req.account:
        if req.account not in known_accounts:
            raise HTTPException(400, f"account {req.account!r} not in the active "
                                f"design (have {known_accounts})")
        account_overrides[host] = req.account
    elif not req.archetype and not req.subnet:
        # no VPC/subnet change and no account given → clear the account pin only
        account_overrides.pop(host, None)
    overrides = dict(nd.overrides or {})
    # a VPC change invalidates any prior subnet pin (different VPC = different
    # subnet names) — drop it so the server lands in its tier subnet in the new
    # VPC, unless the operator also named a subnet valid in the new VPC.
    if req.archetype and req.subnet and req.subnet not in sn_names:
        raise HTTPException(400, f"subnet {req.subnet!r} not in {arch} VPC "
                            f"(have {sorted(sn_names)})")
    if req.archetype and not req.subnet:
        overrides.pop(host, None)
    if req.subnet:
        if req.subnet not in sn_names:
            raise HTTPException(400, f"subnet {req.subnet!r} not in {arch} VPC "
                                f"(have {sorted(sn_names)})")
        overrides[host] = req.subnet
    elif not req.archetype:
        # no VPC change and no subnet given → clear the subnet pin only
        overrides.pop(host, None)
    # clear the VPC pin when the operator sends an empty archetype (revert to the
    # classifier's archetype) and didn't also name a subnet
    if not req.archetype and not req.subnet:
        arch_overrides.pop(host, None)
    rebuilt = build_network_design(lz, servers, STORE.list_matches(),
                                    policy=nd.policy or {}, overrides=overrides,
                                    archetype_overrides=arch_overrides,
                                    account_overrides=account_overrides,
                                    design_id=nd.design_id, name=nd.name)
    # preserve the row's active flag + created_at (build defaults is_active=False /
    # created_at=now, which would deactivate the row on upsert otherwise)
    rebuilt.is_active = nd.is_active
    rebuilt.created_at = nd.created_at
    rebuilt.updated_by = req.by
    STORE.upsert_network_design(rebuilt)
    return {"hostname": host, "placement": rebuilt.placements.get(host, {}),
            "validation": rebuilt.validation, "active": True}


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
    # AGENT tasks (Copilot tab /api/agent) — NOT executor_tasks. The executor
    # pull-dispatch has its own /api/executor/tasks surface. (See the
    # get_agent_task / list_agent_tasks naming note in db.py — the old
    # STORE.list_tasks collided with the executor one and returned the wrong
    # table.)
    return STORE.list_agent_tasks()


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    t = STORE.get_agent_task(task_id)
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
    t = STORE.get_agent_task(task_id)
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
        t = STORE.get_agent_task(task_id)
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