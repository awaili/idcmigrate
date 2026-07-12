"""Mock of the external agent executor.

Implements the executor-side contract from ``docs/agent-executor.md`` so the
whole loop — scan → comb → modify → (raise question) → answer → done — can run
locally without a real external service. It is a tiny FastAPI app that:

* accepts ``POST /v1/scan | /v1/comb | /v1/modify | GET /v1/jobs/{id}``;
* pushes results back to idc-migrate (``POST /api/change-jobs``,
  ``PUT /api/code-profiles/{app_id}``) using the shared bearer token;
* on ``modify``, if a change item has empty ``old``/``new`` (idc-migrate
  couldn't derive the literal/replacement), it raises a Question via
  ``POST /api/apps/{app_id}/questions`` — exactly what a real executor should
  do instead of guessing.

Run: ``python -m idc.executor_mock`` (port 8090) or ``idc executor-mock``.
Config: ``IDC_MOCK_CALLBACK`` (idc-migrate base, default http://localhost:8010),
``IDC_EXECUTOR_TOKEN`` (shared secret, must match idc-migrate).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="idc-executor-mock", version="0.1.0")

CALLBACK = os.getenv("IDC_MOCK_CALLBACK", "http://localhost:8010").rstrip("/")
TOKEN = os.getenv("IDC_EXECUTOR_TOKEN", "")

# in-memory job store (the mock is single-process; fine for local/demo)
_JOBS: Dict[str, Dict[str, Any]] = {}


@app.get("/health")
def health():
    """Liveness + identity for the idc-migrate `doctor` / status-indicator probe."""
    return {"ok": True, "service": "idc-executor-mock", "version": "0.1.0",
            "callback": CALLBACK, "token_configured": bool(TOKEN)}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _jid() -> str:
    return f"cjob-{uuid.uuid4().hex[:10]}"


def _headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


async def _push(path: str, body: Dict[str, Any], method: str = "POST"):
    """Best-effort push to idc-migrate; never raise into the request path."""
    import httpx  # lazy
    url = f"{CALLBACK}{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.request(method, url, json=body, headers=_headers())
    except Exception as e:  # idc-migrate down? log, don't crash the job
        print(f"[mock] push {method} {url} failed: {e!r}")


def _fake_profile(app_id: str, repo_url: str, branch: str, scan_id: str) -> Dict[str, Any]:
    """A plausible CodeProfile so rebuild/wave-plan have something to chew on.

    Includes a hardcoded IP, a DB connection (both with extractable old), and
    a secret whose literal we deliberately *don't* expose — so a later modify
    raises a question (the empty-old path).
    """
    return {
        "app_id": app_id, "repo_url": repo_url, "branch": branch, "scan_id": scan_id,
        "scanner": "idc-executor-mock/0.1.0", "scanned_at": _now(),
        "language": "java", "runtime": "jdk8", "framework": "spring-boot-2.1",
        "cloud_readiness": 0.42, "migration_pattern": "replatform",
        "refactor_effort": "medium",
        "code_deps": ["user-svc", "inventory-svc"],
        "network_endpoints": ["10.0.4.20:3306", "redis01.dc1:6379"],
        "required_changes": [
            {"title": "Parameterize DB host", "category": "db_connection",
             "file": "src/main/resources/application.yml", "line": 12, "effort": "low",
             "description": "Replace hardcoded 10.0.4.20 with ${DB_HOST}"},
            {"title": "Move DB password to secret ref", "category": "secrets_in_repo",
             "file": "src/main/resources/application.yml", "line": 13, "effort": "low",
             "description": "Inline password → ${DB_PASSWORD}"},
        ],
        "blockers": ["Quartz scheduler assumes host crontab (scheduled_job, blocker)"],
        "findings": [
            {"category": "db_connection", "severity": "high",
             "file": "src/main/resources/application.yml", "line": 12,
             "message": "hardcoded DB host",
             "evidence": "url: jdbc:mysql://10.0.4.20:3306/orders",
             "remediation": "use ${DB_HOST} from env"},
            {"category": "secrets_in_repo", "severity": "high",
             "file": "src/main/resources/application.yml", "line": 13,
             "message": "committed secret (literal redacted)",
             # evidence deliberately has no password=/secret= token, so the
             # extractor returns "" → the change is unresolved → a later
             # modify raises a Question (the empty-old path we want to demo).
             "evidence": "•••••• (redacted — literal not stored in evidence)",
             "remediation": "move to ${DB_PASSWORD} secret ref"},
        ],
        "summary": "Spring Boot 2.1 on JDK8; needs DB host param + service-discovery swap.",
    }


# ---------------------------------------------------------------------------
# request schemas
# ---------------------------------------------------------------------------
class ScanReq(BaseModel):
    app_id: str
    repo_url: str
    branch: str = ""
    callback: str = ""


class DBScanReq(BaseModel):
    db_server_id: str
    source_engine: str = ""
    target_engine: str = ""
    callback: str = ""


class RuntimeContainerizeReq(BaseModel):
    app_id: str
    server_id: str
    inventory: Dict[str, Any] = {}
    mode: str = "plan"        # plan (dry-run scaffold) | execute (write it)
    callback: str = ""


class ModifyReq(BaseModel):
    app_id: str
    repo_url: str
    branch: str = ""
    mode: str = "plan"
    scope: List[str] = []
    changes: List[Dict[str, Any]] = []
    notes: List[str] = []


def _check_auth(authorization: Optional[str]):
    if not TOKEN:
        return  # no token configured → open (local dev)
    if not (authorization or "").startswith("Bearer "):
        raise HTTPException(401, "missing bearer")
    if authorization.split(" ", 1)[1].strip() != TOKEN:
        raise HTTPException(401, "bad token")


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@app.post("/v1/scan", status_code=202)
async def v1_scan(req: ScanReq, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.app_id, "kind": "scan", "repo_url": req.repo_url,
                  "branch": req.branch, "status": "pending", "created_at": _now()}
    asyncio.create_task(_run_scan(req, jid))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/comb", status_code=202)
async def v1_comb(req: ScanReq, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.app_id, "kind": "comb", "repo_url": req.repo_url,
                  "branch": req.branch, "status": "pending", "created_at": _now()}
    asyncio.create_task(_run_scan(req, jid))  # comb produces a profile too
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/db-scan", status_code=202)
async def v1_db_scan(req: DBScanReq, authorization: Optional[str] = Header(None)):
    """F5 — trigger a heterogeneous DB-scan. The mock derives a plausible
    DBConversionProfile from the source engine and pushes it back to
    ``PUT /api/db-profiles/{db_server_id}`` so the F5 enrich loop is testable
    end-to-end without a real executor."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.db_server_id, "kind": "db-scan",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_db_scan(req, jid))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/runtime-containerize", status_code=202)
async def v1_runtime_containerize(req: RuntimeContainerizeReq,
                                  authorization: Optional[str] = Header(None)):
    """F9 — no-source containerization. The mock infers a Dockerfile scaffold +
    a CodeProfile tagged ``source=runtime-derived`` from the runtime inventory
    (process/port/software) and pushes the profile back to
    ``PUT /api/code-profiles/{app_id}``. The patch_ref is a Dockerfile scaffold."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.app_id, "kind": "runtime-containerize",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_runtime_containerize(req, jid))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/modify", status_code=202)
async def v1_modify(req: ModifyReq, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.app_id, "kind": "modify", "repo_url": req.repo_url,
                  "branch": req.branch, "status": "pending", "created_at": _now()}
    asyncio.create_task(_run_modify(req, jid))
    return {"job_id": jid, "status": "pending"}


@app.get("/v1/jobs/{job_id}")
def v1_job(job_id: str):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return j


# ---------------------------------------------------------------------------
# background runners (push results back to idc-migrate)
# ---------------------------------------------------------------------------
async def _run_scan(req: ScanReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running", "summary": "scanning…"})
    await asyncio.sleep(0.5)  # pretend to work
    profile = _fake_profile(req.app_id, req.repo_url, req.branch, jid)
    job.update(status="done", finished_at=_now(),
               summary=f"scanned {req.app_id}; 2 findings, pattern=replatform")
    await _push("/api/change-jobs", {**job, "status": "done"})
    await _push(f"/api/code-profiles/{req.app_id}", profile, method="PUT")


def _fake_db_profile(db_server_id: str, source_engine: str,
                     target_engine: str, scan_id: str) -> Dict[str, Any]:
    """A plausible DBConversionProfile (F5). Oracle→TDSQL is the hard case
    (PL/SQL → grade C + reverse replication); SQLServer→CDB is medium; a
    mysql-class source is easy (grade A). Mirrors the Ora2Pg A–C grading +
    AWS DMS SC auto-convert percentage."""
    src = (source_engine or "").lower()
    if "oracle" in src:
        return {
            "db_server_id": db_server_id, "source_engine": "oracle",
            "target_engine": target_engine or "tdsql", "difficulty": "C",
            "est_man_days": 40.0,
            "review_objects": ["PKG_ORDERS", "PKG_BILLING", "TRG_AUDIT_LOG"],
            "blockers": ["PL/SQL packages with no MySQL equivalent (DBMS_LOCK, UTL_FILE)"],
            "reverse_replication": True, "auto_convert_pct": 0.62, "scan_id": scan_id,
            "scanned_at": _now(),
            "summary": "Oracle → TDSQL: heavy PL/SQL, hard conversion, reverse replication recommended.",
        }
    if "sqlserver" in src or "sql server" in src:
        return {
            "db_server_id": db_server_id, "source_engine": "sqlserver",
            "target_engine": target_engine or "cdb_mysql", "difficulty": "B",
            "est_man_days": 12.0,
            "review_objects": ["sp_report_rollup", "vew_daily_stats"],
            "blockers": ["T-SQL stored procs using ##temp tables"],
            "reverse_replication": False, "auto_convert_pct": 0.85, "scan_id": scan_id,
            "scanned_at": _now(),
            "summary": "SQLServer → CDB MySQL: medium, manual review of a few procs.",
        }
    # mysql-class source — easy
    return {
        "db_server_id": db_server_id, "source_engine": src or "mysql",
        "target_engine": target_engine or "cdb_mysql", "difficulty": "A",
        "est_man_days": 2.0, "review_objects": [], "blockers": [],
        "reverse_replication": False, "auto_convert_pct": 0.97, "scan_id": scan_id,
        "scanned_at": _now(),
        "summary": "MySQL-class → CDB: easy, near-fully auto-convertible.",
    }


async def _run_db_scan(req: DBScanReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running", "summary": "db-scanning…"})
    await asyncio.sleep(0.5)
    prof = _fake_db_profile(req.db_server_id, req.source_engine,
                            req.target_engine, jid)
    job.update(status="done", finished_at=_now(),
               summary=f"db-scan {req.db_server_id}; difficulty={prof['difficulty']}")
    await _push("/api/change-jobs", {**job, "status": "done"})
    await _push(f"/api/db-profiles/{req.db_server_id}", prof, method="PUT")


def _fake_runtime_profile(app_id: str, server_id: str, scan_id: str) -> Dict[str, Any]:
    """F9 — a plausible runtime-derived CodeProfile (no source repo). Inferred
    from a process+port+software inventory: e.g. a Java app on port 8080 with a
    systemd unit → a Dockerfile scaffold (openjdk base + CMD the jar). Tagged
    ``source=runtime-derived`` so confidence is capped + a confirm-gate fires
    before modify."""
    return {
        "app_id": app_id, "repo_url": "", "branch": "", "scan_id": scan_id,
        "scanner": "idc-executor-mock/0.1.0", "scanned_at": _now(),
        "language": "java", "runtime": "jdk8", "framework": "spring-boot (inferred)",
        "cloud_readiness": 0.5, "migration_pattern": "rehost-container",
        "refactor_effort": "medium",
        "code_deps": [], "network_endpoints": ["0.0.0.0:8080"],
        "required_changes": [
            {"title": "Generate Dockerfile scaffold from runtime inventory",
             "category": "config_coupling", "file": "Dockerfile", "line": 1,
             "effort": "medium",
             "description": "FROM openjdk:8-jre-slim; COPY the inferred jar; "
                            "EXPOSE 8080; CMD the discovered start command"}
        ],
        "blockers": [],
        "findings": [
            {"category": "config_coupling", "severity": "medium", "file": "(runtime)",
             "line": 0, "message": "no source repo — profile inferred from telemetry",
             "evidence": "process=java -jar app.jar, port=8080, unit=app.service",
             "remediation": "operator must confirm the inferred Dockerfile before apply"}
        ],
        "summary": "Runtime-derived: Java app on :8080 (systemd) → openjdk:8-jre-slim scaffold.",
        "source": "runtime-derived",
    }


async def _run_runtime_containerize(req: RuntimeContainerizeReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running",
                                      "summary": "inferring scaffold from runtime…"})
    await asyncio.sleep(0.5)
    prof = _fake_runtime_profile(req.app_id, req.server_id, jid)
    patch = f"dockerfile-scaffold-{jid}" if req.mode == "plan" else f"dockerfile-pr-{jid}"
    job.update(status="done", finished_at=_now(), patch_ref=patch,
               summary=f"runtime-containerize {req.mode}: inferred Dockerfile scaffold")
    await _push("/api/change-jobs", {**job, "status": "done", "patch_ref": patch})
    await _push(f"/api/code-profiles/{req.app_id}", prof, method="PUT")


async def _run_modify(req: ModifyReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running", "summary": "modifying…"})
    await asyncio.sleep(0.5)

    # for any change the caller couldn't fully resolve (empty old/new), raise a
    # question — the operator answers, and a real executor would poll for it.
    unresolved = [c for c in req.changes if not (c.get("old") or "").strip()
                 or not (c.get("new") or "").strip()]
    for c in unresolved:
        ctx = {"category": c.get("category"), "kind": c.get("kind"),
               "file": c.get("file"), "line": c.get("line"),
               "old": c.get("old", ""), "new": c.get("new", ""),
               "title": c.get("title", ""), "evidence": c.get("evidence", "")}
        prompt = (f"What should {c.get('old') or 'this token'!r} in "
                  f"{c.get('file')}:{c.get('line') or '?'} ({c.get('category')}) become?")
        await _push(f"/api/apps/{req.app_id}/questions", {
            "job_id": jid, "kind": "choice", "prompt": prompt,
            "options": [c.get("new") or "${PLACEHOLDER}"], "context": ctx})

    patch = f"mock-patch-{jid}" if req.mode == "plan" else f"mock-pr-{jid}"
    job.update(status="done", finished_at=_now(), patch_ref=patch,
               summary=f"modify {req.mode}: {len(req.changes)} change(s), "
                       f"{len(unresolved)} question(s) raised")
    await _push("/api/change-jobs", {**job, "status": "done", "patch_ref": patch})


def main():
    import uvicorn
    uvicorn.run("idc.executor_mock.app:app", host="0.0.0.0", port=8090,
                reload=False, log_level="info")


if __name__ == "__main__":
    main()