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
            r = await c.request(method, url, json=body, headers=_headers())
            # httpx does NOT raise on 4xx/5xx by default — a 401 (token mismatch)
            # or 400/500 would otherwise silently drop the profile/results and the
            # job would look green while idc-migrate got nothing. Log non-2xx.
            if r.status_code >= 400:
                print(f"[mock] push {method} {url} -> HTTP {r.status_code}: "
                      f"{r.text[:200]}")
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
    mode: str = "assess"      # assess (grade-only) | convert (also emit DDL + report)
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


class LegacyDispositionReq(BaseModel):
    server_id: str
    context: Dict[str, Any] = {}
    callback: str = ""


class DocReq(BaseModel):
    wave_id: str
    context: Dict[str, Any] = {}
    callback: str = ""


class IacEmitReq(BaseModel):
    scope: str            # landing_zone | workload
    scope_id: str         # lz:<arch> | wl:<server_id>
    context: Dict[str, Any] = {}
    callback: str = ""


class PostMigReq(BaseModel):
    server_id: str
    context: Dict[str, Any] = {}
    callback: str = ""


class TestGenReq(BaseModel):
    app_id: str
    context: Dict[str, Any] = {}
    callback: str = ""


class TestRunReq(BaseModel):
    app_id: str
    phase: str          # pre | post
    target: str         # endpoint base to hit
    run_id: str = ""    # optional pre-set id (idc-migrate can mint it)
    context: Dict[str, Any] = {}
    callback: str = ""


class TestCompareReq(BaseModel):
    app_id: str
    pre_run_id: str
    post_run_id: str
    callback: str = ""


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


@app.post("/v1/legacy-disposition", status_code=202)
async def v1_legacy_disposition(req: LegacyDispositionReq,
                                authorization: Optional[str] = Header(None)):
    """F7 — analyze an EOL/unsupported-OS host and recommend
    containerize/replatform/rewrite/retain. The mock decides from the workload
    context (role / has_source_repo / runtime_inventory) and pushes a
    LegacyDisposition back to ``PUT /api/legacy-dispositions/{server_id}``."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.server_id, "kind": "legacy-disposition",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_legacy_disposition(req, jid))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/cutover-playbook", status_code=202)
async def v1_cutover_playbook(req: DocReq, authorization: Optional[str] = Header(None)):
    """F9 — generate a per-wave cutover playbook (ordered per-server cutover
    steps) from the wave's members + downtime window + risk basis. Pushes a
    DocArtifact (markdown) back to ``PUT /api/docs/cutover/{wave_id}``."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.wave_id, "kind": "cutover-playbook",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_doc(req, jid, "cutover", _fake_cutover_playbook))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/as-built", status_code=202)
async def v1_as_built(req: DocReq, authorization: Optional[str] = Header(None)):
    """F9 — generate a per-wave as-built doc from the executed stage_history,
    gate results, and change jobs. Pushes a DocArtifact (markdown) back to
    ``PUT /api/docs/as-built/{wave_id}``."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.wave_id, "kind": "as-built",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_doc(req, jid, "as_built", _fake_as_built))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/iac-emit", status_code=202)
async def v1_iac_emit(req: IacEmitReq, authorization: Optional[str] = Header(None)):
    """F5 — emit landing-zone / workload Terraform + run Well-Architected
    guardrail checks. The mock emits 2-3 ``resource "tencentcloud_*"`` blocks
    from the blueprint/match + 2 guardrail checks (one pass, one fail) so the
    launch gate is exercisable. Pushes an IaCArtifact back to
    ``PUT /api/iac-artifacts/{scope_id}``."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.scope_id, "kind": "iac-emit",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_iac_emit(req, jid))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/postmig-optimize", status_code=202)
async def v1_postmig_optimize(req: PostMigReq,
                              authorization: Optional[str] = Header(None)):
    """F10 — analyze a finalized host's post-mig cloud metrics + return
    recommendations (right_size / reserved / anomaly / perf). The mock derives
    one rec of each kind from the target spec + a canned metrics window and
    pushes them back to ``PUT /api/postmig-recs/{server_id}/{kind}``."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.server_id, "kind": "postmig-optimize",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_postmig_optimize(req, jid))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/test-gen", status_code=202)
async def v1_test_gen(req: TestGenReq, authorization: Optional[str] = Header(None)):
    """F11 — generate test cases from the app's CodeProfile. The mock emits a
    health-check case + one endpoint case and pushes them to
    ``PUT /api/test-cases/{app_id}`` (a list)."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.app_id, "kind": "test-gen",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_test_gen(req, jid))
    return {"job_id": jid, "status": "pending"}


@app.post("/v1/test-run", status_code=202)
async def v1_test_run(req: TestRunReq, authorization: Optional[str] = Header(None)):
    """F11 — run an app's test set against a target (pre on-prem / post cloud).
    The mock derives pass/fail outcomes from the phase + target and pushes a
    TestRun to ``PUT /api/test-runs/{run_id}``."""
    _check_auth(authorization)
    jid = _jid()
    run_id = req.run_id or f"trun-{jid[5:]}"
    _JOBS[jid] = {"id": jid, "app_id": req.app_id, "kind": "test-run",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_test_run(req, jid, run_id))
    return {"job_id": jid, "status": "pending", "run_id": run_id}


@app.post("/v1/test-compare", status_code=202)
async def v1_test_compare(req: TestCompareReq,
                          authorization: Optional[str] = Header(None)):
    """F11 — diff a pre-run vs a post-run. The mock derives per-case verdicts
    (one regression to exercise the gate) and pushes a TestDiff to
    ``PUT /api/test-diffs/{app_id}``."""
    _check_auth(authorization)
    jid = _jid()
    _JOBS[jid] = {"id": jid, "app_id": req.app_id, "kind": "test-compare",
                  "repo_url": "", "branch": "", "status": "pending",
                  "created_at": _now()}
    asyncio.create_task(_run_test_compare(req, jid))
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


def _fake_db_conversion(db_server_id: str, source_engine: str,
                        target_engine: str, scan_id: str) -> Dict[str, Any]:
    """F6 — a plausible converted-schema artifact + per-object compatibility
    report. Emits converted DDL + one object per conversion outcome (auto /
    review / blocked) so the compatibility report is actionable. Includes a
    PostgreSQL target path (Oracle→PG) so the missing target is closed."""
    src = (source_engine or "").lower()
    tgt = (target_engine or "").lower()
    if "oracle" in src and "postg" in tgt:
        objs = [
            {"name": "ORDERS", "kind": "table", "status": "auto_converted",
             "converted": "CREATE TABLE orders (id BIGINT PRIMARY KEY, ...);",
             "issue": "", "effort_days": 0.0},
            {"name": "V_DAILY_STATS", "kind": "view", "status": "auto_converted",
             "converted": "CREATE VIEW v_daily_stats AS SELECT ...;",
             "issue": "", "effort_days": 0.0},
            {"name": "SEQ_ORDERS", "kind": "sequence", "status": "auto_converted",
             "converted": "CREATE SEQUENCE seq_orders;  -- IDENTITY column",
             "issue": "", "effort_days": 0.0},
            {"name": "PKG_ORDERS", "kind": "package", "status": "manual_review",
             "converted": "-- partially converted PL/pgSQL function",
             "issue": "DBMS_LOCK.SLEEP has no PG equivalent; replace with pg_sleep",
             "effort_days": 3.0},
            {"name": "PKG_BILLING", "kind": "package", "status": "blocked",
             "converted": "", "issue": "UTL_FILE writes to server filesystem — rewrite as a sidecar service",
             "effort_days": 8.0},
            {"name": "TRG_AUDIT_LOG", "kind": "trigger", "status": "auto_converted",
             "converted": "CREATE TRIGGER trg_audit_log ...;", "issue": "", "effort_days": 0.0},
        ]
        ddl = [o["converted"] for o in objs if o["converted"]]
        auto = sum(1 for o in objs if o["status"] == "auto_converted")
        return {"target_engine": "postgresql", "ddl": ddl, "objects": objs,
                "auto_convert_pct": round(auto / len(objs), 2),
                "report_md": _db_report_md("postgresql", objs)}
    if "oracle" in src:
        objs = [
            {"name": "ORDERS", "kind": "table", "status": "auto_converted",
             "converted": "CREATE TABLE orders (...) ENGINE=InnoDB;", "issue": "", "effort_days": 0.0},
            {"name": "PKG_ORDERS", "kind": "package", "status": "blocked",
             "converted": "", "issue": "PL/SQL packages with no MySQL equivalent (DBMS_LOCK, UTL_FILE)",
             "effort_days": 15.0},
            {"name": "TRG_AUDIT_LOG", "kind": "trigger", "status": "manual_review",
             "converted": "CREATE TRIGGER trg_audit_log ...;",
             "issue": "compound trigger → split into row + statement triggers", "effort_days": 2.0},
        ]
    elif "sqlserver" in src:
        objs = [
            {"name": "orders", "kind": "table", "status": "auto_converted",
             "converted": "CREATE TABLE orders (...) ENGINE=InnoDB;", "issue": "", "effort_days": 0.0},
            {"name": "sp_report_rollup", "kind": "package", "status": "manual_review",
             "converted": "CREATE PROCEDURE sp_report_rollup() BEGIN ... END;",
             "issue": "##temp tables → MySQL TEMPORARY TABLE", "effort_days": 2.0},
            {"name": "vew_daily_stats", "kind": "view", "status": "auto_converted",
             "converted": "CREATE VIEW vew_daily_stats AS SELECT ...;", "issue": "", "effort_days": 0.0},
        ]
    else:
        objs = [
            {"name": "orders", "kind": "table", "status": "auto_converted",
             "converted": "CREATE TABLE orders (...);", "issue": "", "effort_days": 0.0},
            {"name": "v_daily", "kind": "view", "status": "auto_converted",
             "converted": "CREATE VIEW v_daily AS SELECT ...;", "issue": "", "effort_days": 0.0},
        ]
    auto = sum(1 for o in objs if o["status"] == "auto_converted")
    ddl = [o["converted"] for o in objs if o["converted"]]
    return {"target_engine": target_engine or "cdb_mysql", "ddl": ddl,
            "objects": objs, "auto_convert_pct": round(auto / len(objs), 2),
            "report_md": _db_report_md(target_engine or "cdb_mysql", objs)}


def _db_report_md(target_engine: str, objs: List[Dict[str, Any]]) -> str:
    """Markdown compatibility report from the per-object outcomes."""
    auto = sum(1 for o in objs if o["status"] == "auto_converted")
    review = sum(1 for o in objs if o["status"] == "manual_review")
    blocked = sum(1 for o in objs if o["status"] == "blocked")
    lines = [f"# DB conversion compatibility report (→ {target_engine})",
             "", f"- {len(objs)} objects: {auto} auto, {review} review, {blocked} blocked",
             f"- auto-convert: {round(auto/len(objs), 2)*100:.0f}%", "", "## Objects", "",
             "| Object | Kind | Status | Issue | Effort (d) |",
             "|---|---|---|---|---|"]
    for o in objs:
        lines.append(f"| {o['name']} | {o['kind']} | {o['status']} | "
                     f"{o['issue'] or '—'} | {o['effort_days']} |")
    return "\n".join(lines)


async def _run_db_scan(req: DBScanReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running", "summary": "db-scanning…"})
    await asyncio.sleep(0.5)
    prof = _fake_db_profile(req.db_server_id, req.source_engine,
                            req.target_engine, jid)
    # F6 — in convert mode, also attach the converted-schema artifact + the
    # per-object compatibility report. assess mode stays grade-only (back-compat).
    if (req.mode or "assess").lower() == "convert":
        prof["conversion"] = _fake_db_conversion(req.db_server_id, req.source_engine,
                                                 req.target_engine, jid)
    job.update(status="done", finished_at=_now(),
               summary=f"db-scan {req.db_server_id}; difficulty={prof['difficulty']}"
                       + (" (+conversion)" if "conversion" in prof else ""))
    await _push("/api/change-jobs", {**job, "status": "done"})
    await _push(f"/api/db-profiles/{req.db_server_id}", prof, method="PUT")


def _fake_legacy_disposition(server_id: str, ctx: Dict[str, Any],
                             scan_id: str) -> Dict[str, Any]:
    """F7 — a plausible containerize/replatform/rewrite/retain recommendation
    derived from the workload context. Decision logic (mirrors what a real
    executor's analyst agent would do):
      * has_source_repo=False + stateless web/app role → containerize (no
        source, runtime inventory is enough for a Dockerfile scaffold).
      * has_source_repo=True + stateful role (db/cache) → rewrite (tightly
        coupled to the legacy runtime; re-architect onto managed service).
      * otherwise (source exists, app role, EOL) → replatform (swap the base
        image / managed runtime, keep the shape).
      * regulated / mainframe tags → retain.
    """
    role = (ctx.get("role") or "").lower()
    has_repo = bool(ctx.get("has_source_repo"))
    tags = [t.lower() for t in (ctx.get("tags") or [])]
    runtime = (ctx.get("runtime") or "").lower()
    os_bucket = ctx.get("os_eol_bucket") or "expired"
    if any(t in tags for t in ("regulated", "mainframe", "retain")):
        return _ld(server_id, "retain", "regulated/mainframe workload — keep on-prem",
                   0.8, "", 0.0, ["regulatory sign-off before any change"], scan_id,
                   f"retain (regulated) on {ctx.get('os','')}")
    if not has_repo and role in ("app", "web", "frontend"):
        return _ld(server_id, "containerize",
                   "stateless tier with no source repo — infer a Dockerfile from runtime inventory",
                   0.7, "tencentos-server-3.1:jdk17", 5.0,
                   ["capture runtime inventory (process/port/software)",
                    "validate the inferred port reaches"], scan_id,
                   f"containerize {role} on a supported base image")
    if has_repo and role in ("db", "cache", "data"):
        return _ld(server_id, "rewrite",
                   f"stateful {role} tightly coupled to legacy runtime — re-architect onto a managed service",
                   0.6, "", 25.0,
                   ["extract schema + data", "rewrite data-access layer",
                    "dual-run + reverse replication during cutover"], scan_id,
                   f"rewrite {role} onto managed service")
    # Windows hosts can't run a Linux TencentOS base image; pick a Windows base
    # so the replatform recommendation is actually runnable on the source OS.
    base = ("tencentos-server-3.1" if "windows" not in (ctx.get("os") or "").lower()
            else "windowsservercore-1809")
    return _ld(server_id, "replatform",
               f"source exists, app role — swap the {os_bucket} base image + keep the shape",
               0.75, f"{base}:jdk17", 8.0,
               ["update CI base image", "smoke-test on the new image"], scan_id,
               f"replatform onto {base}")


def _ld(server_id, disposition, rationale, conf, image, effort, prereqs,
        scan_id, summary):
    return {"server_id": server_id, "disposition": disposition,
            "rationale": rationale, "confidence": conf,
            "target_base_image": image, "effort_days": effort,
            "prereqs": prereqs, "scan_id": scan_id, "scanned_at": _now(),
            "summary": summary}


async def _run_legacy_disposition(req: LegacyDispositionReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running",
                                     "summary": "analyzing EOL workload…"})
    await asyncio.sleep(0.4)
    disp = _fake_legacy_disposition(req.server_id, req.context, jid)
    job.update(status="done", finished_at=_now(),
               summary=f"legacy-disposition {req.server_id}; {disp['disposition']}")
    await _push("/api/change-jobs", {**job, "status": "done"})
    await _push(f"/api/legacy-dispositions/{req.server_id}", disp, method="PUT")


# F9 — cutover playbook / as-built generators. The mock builds templated
# markdown from the inputs idc-migrate sends; a real executor uses its LLM to
# narrate, but the SHAPE (ordered per-server steps / what-moved-where) is the
# same and is grounded in the wave's real data.
def _fake_cutover_playbook(wave_id: str, ctx: Dict[str, Any]) -> str:
    members = ctx.get("members") or []
    window = ctx.get("downtime_window") or {}
    risk = ctx.get("risk_basis") or {}
    lines = [f"# Cutover playbook — wave {wave_id}", ""]
    if window.get("start"):
        lines.append(f"**Downtime window:** {window['start']} → {window.get('end','')}")
    if risk.get("level"):
        lines.append(f"**Risk:** {risk.get('level','')} "
                     f"(score {risk.get('score','')})")
    lines += ["", "## Pre-cutover (T-24h)", "",
              "- freeze changes; confirm reverse-replication healthy for DB hosts",
              "- snapshot all wave members; broadcast comms window", ""]
    lines += ["## Cutover (in order)", ""]
    for i, m in enumerate(members, 1):
        role = m.get("role", "host")
        tgt = m.get("target", "?")
        sid = m.get("server_id", "?")
        lines.append(f"{i}. **{sid}** ({role} → {tgt})")
        if role == "db":
            lines.append(f"   - cut writes; drain + final DTS sync; cut replicas; "
                         f"flip app connection strings to {tgt}")
            if m.get("reverse_replication"):
                lines.append("   - keep reverse-replication channel open until finalize")
        else:
            lines.append(f"   - stop on-prem; provision {tgt}; restore snapshot; "
                         f"smoke-test ports {m.get('ports', [])}")
        lines.append("")
    lines += ["## Rollback", "",
              "- per server: revert DNS / connection string + restart on-prem; "
              "for DB hosts, fail over via the reverse-replication channel", ""]
    return "\n".join(lines)


def _fake_as_built(wave_id: str, ctx: Dict[str, Any]) -> str:
    targets = ctx.get("targets") or []
    gates = ctx.get("gate_results") or []
    jobs = ctx.get("change_jobs") or []
    stages = ctx.get("stage_history") or []
    lines = [f"# As-built — wave {wave_id}", "",
             f"- executed stages: {len(stages)}; gate results: {len(gates)}; "
             f"change jobs: {len(jobs)}", "", "## What moved where", "",
             "| Server | Role | Target | Final spec |", "|---|---|---|---|"]
    for t in targets:
        lines.append(f"| {t.get('server_id','?')} | {t.get('role','?')} | "
                      f"{t.get('product','?')} | {t.get('spec','?')} |")
    lines += ["", "## Gate outcomes", ""]
    for g in gates:
        lines.append(f"- {g.get('name','?')}: **{g.get('status','?')}**"
                     + (f" — {g['detail']}" if g.get("detail") else ""))
    lines += ["", "## Change jobs (patch refs)", ""]
    for j in jobs:
        lines.append(f"- {j.get('app_id','?')}: `{j.get('patch_ref','')}` "
                     f"({j.get('status','')})")
    if not jobs:
        lines.append("- (none)")
    return "\n".join(lines)


async def _run_doc(req: DocReq, jid: str, doc_type: str, gen):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running",
                                     "summary": f"generating {doc_type}…"})
    await asyncio.sleep(0.4)
    md = gen(req.wave_id, req.context)
    job.update(status="done", finished_at=_now(),
               summary=f"{doc_type} for {req.wave_id}")
    await _push("/api/change-jobs", {**job, "status": "done"})
    slug = "as-built" if doc_type == "as_built" else doc_type
    await _push(f"/api/docs/{slug}/{req.wave_id}", {
        "doc_type": doc_type, "scope_id": req.wave_id, "doc_md": md,
        "scan_id": jid, "scanned_at": _now(),
        "summary": f"{doc_type} for wave {req.wave_id}",
    }, method="PUT")


# F5 — IaC generator. The mock emits real ``resource "tencentcloud_*"`` HCL
# blocks from the blueprint (landing_zone) or match (workload) + runs 2
# guardrail checks (one pass, one fail) so the launch gate is exercisable.
# A real executor emits via its IaC library + a real guardrail engine (OPA /
# terraform-plan scan); the SHAPE here is the contract.
def _fake_iac_artifact(scope: str, scope_id: str, ctx: Dict[str, Any],
                      scan_id: str) -> Dict[str, Any]:
    arch = scope_id.split(":", 1)[1] if scope_id.startswith("lz:") else ""
    blueprint = ctx.get("blueprint") or {}
    vpc = (blueprint.get("vpc") or {}) if blueprint else {"name": "vpc-corp", "cidr": "10.10.0.0/16"}
    modules = [
        {"path": "main.tf",
         "content": (
             'resource "tencentcloud_vpc" "main" {\n'
             f'  name = "{vpc.get("name", "vpc-corp")}"\n'
             f'  cidr_block = "{vpc.get("cidr", "10.10.0.0/16")}"\n'
             '}\n'
             'resource "tencentcloud_subnet" "app" {\n'
             '  vpc_id = tencentcloud_vpc.main.id\n'
             '  cidr_block = "10.10.1.0/24"\n'
             '}\n')},
        {"path": "variables.tf",
         "content": 'variable "region" { default = "ap-bangkok" }\n'},
    ]
    # guardrails derived from the blueprint's policy_as_code (the rule ids).
    policy = blueprint.get("policy_as_code") or []
    guardrails = []
    # pass the security rule; WARN on the cost rule so the gate is PASSABLE by
    # default (warns don't block, only medium/high fails do). A blocking fail
    # would make the mock-guardrail gate unpassable — the demo could never
    # launch a workload wave. Tests push their own medium-fail to exercise
    # the block path.
    guardrails.append({"pillar": "security", "rule": "no_public_clb_in_corp",
                       "status": "pass", "finding": "", "severity": "high"})
    guardrails.append({"pillar": "cost", "rule": "required_tags",
                       "status": "warn",
                       "finding": f"{vpc.get('name','vpc-corp')} cost_center tag recommended",
                       "severity": "low"})
    if policy:
        guardrails.append({"pillar": "security", "rule": policy[0][:40],
                           "status": "pass", "finding": "", "severity": "high"})
    failing = [g for g in guardrails
              if g["status"] == "fail" and g["severity"] in ("high", "medium")]
    return {"scope_id": scope_id, "scope": scope, "modules": modules,
            "guardrails": guardrails, "guardrail_pass": not failing,
            "plan_summary": "Plan: 2 to add, 0 to change, 0 to destroy.",
            "target": ctx.get("target") or {"cloud": "tencent", "region": "ap-bangkok"},
            "scan_id": scan_id, "scanned_at": _now(),
            "summary": f"IaC for {scope_id}"}


async def _run_iac_emit(req: IacEmitReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running",
                                     "summary": "emitting IaC + guardrails…"})
    await asyncio.sleep(0.5)
    art = _fake_iac_artifact(req.scope, req.scope_id, req.context, jid)
    job.update(status="done", finished_at=_now(),
               summary=f"iac-emit {req.scope_id}; pass={art['guardrail_pass']}")
    await _push("/api/change-jobs", {**job, "status": "done"})
    await _push(f"/api/iac-artifacts/{req.scope_id}", art, method="PUT")


# F10 — post-mig recommendations. The mock derives one rec of each kind from
# the host's matched target + a canned metrics window. A real executor pulls
# the now-running cloud resource's metrics from Cloud Monitor / Prometheus and
# reasons about them; the SHAPE (right_size/reserved/anomaly/perf + savings) is
# the contract.
def _fake_postmig_recs(server_id: str, ctx: Dict[str, Any],
                       scan_id: str) -> List[Dict[str, Any]]:
    spec = (ctx.get("target") or {}).get("spec") or "SA2.LARGE8"
    product = (ctx.get("target") or {}).get("product") or "CVM"
    metrics = ctx.get("metrics") or {"cpu_p95": 8, "mem_p95": 22, "uptime_pct": 99.5}
    recs = []
    # right-size down (low utilization)
    if metrics.get("cpu_p95", 0) < 30:
        recs.append({"server_id": server_id, "kind": "right_size",
                     "from_spec": spec, "to_spec": "SA2.MEDIUM4",
                     "reason": f"p95 cpu {metrics['cpu_p95']}% over 30d — under-utilized",
                     "monthly_saving_usd": 120.0, "confidence": 0.8,
                     "severity": "low", "detail": "", "scan_id": scan_id,
                     "scanned_at": _now(), "summary": "right-size down"})
    # reserved (steady-state)
    if metrics.get("uptime_pct", 0) > 80:
        recs.append({"server_id": server_id, "kind": "reserved",
                     "from_spec": spec, "to_spec": "1yr",
                     "reason": f"steady-state uptime {metrics['uptime_pct']}% → 1yr reserved",
                     "monthly_saving_usd": 60.0, "confidence": 0.7,
                     "severity": "low", "detail": "", "scan_id": scan_id,
                     "scanned_at": _now(), "summary": "move to 1yr reserved"})
    # anomaly (a spike to investigate)
    recs.append({"server_id": server_id, "kind": "anomaly",
                 "from_spec": "", "to_spec": "",
                 "reason": "cpu spike outlier",
                 "monthly_saving_usd": 0.0, "confidence": 0.6,
                 "severity": "medium",
                 "detail": "spike 2026-07-10T03:00Z — investigate deploy/job",
                 "scan_id": scan_id, "scanned_at": _now(),
                 "summary": "cpu anomaly to investigate"})
    # perf tuning
    recs.append({"server_id": server_id, "kind": "perf",
                 "from_spec": "", "to_spec": "",
                 "reason": "DB connection pool saturated",
                 "monthly_saving_usd": 0.0, "confidence": 0.65,
                 "severity": "medium",
                 "detail": "pool at 95% during peak — raise pool size or add read replica",
                 "scan_id": scan_id, "scanned_at": _now(),
                 "summary": "tune DB connection pool"})
    return recs


async def _run_postmig_optimize(req: PostMigReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running",
                                     "summary": "analyzing post-mig metrics…"})
    await asyncio.sleep(0.5)
    recs = _fake_postmig_recs(req.server_id, req.context, jid)
    job.update(status="done", finished_at=_now(),
               summary=f"postmig-optimize {req.server_id}; {len(recs)} recs")
    await _push("/api/change-jobs", {**job, "status": "done"})
    for r in recs:
        await _push(f"/api/postmig-recs/{req.server_id}/{r['kind']}", r, method="PUT")


# F11 — test case generation + run + compare. The mock emits plausible cases
# from the CodeProfile summary, derives pass/fail from phase+target, and
# produces one regression in the diff to exercise the cutover gate.
def _fake_test_cases(app_id: str, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    # The integration case name is STABLE (not framework-derived) so the gen,
    # run and compare steps all reference the same case — _fake_test_results and
    # _run_test_compare build names from _fake_test_cases(app_id, {}) which
    # defaults framework to "app", so a framework-specific name here would make
    # the persisted TestCase list disagree with the TestRun/TestDiff case names.
    return [
        {"app_id": app_id, "name": "health", "kind": "functional",
         "endpoint": "/health", "method": "GET", "request": {},
         "expected": {"status": 200, "body_contains": "ok"},
         "setup": "none", "scan_id": "", "updated_at": _now()},
        {"app_id": app_id, "name": "app_index_200", "kind": "integration",
         "endpoint": "/", "method": "GET", "request": {},
         "expected": {"status": 200}, "setup": "seed minimal fixture",
         "scan_id": "", "updated_at": _now()},
        {"app_id": app_id, "name": "regression_orders_total", "kind": "regression",
         "endpoint": "/orders/total", "method": "GET", "request": {},
         "expected": {"status": 200, "body_contains": "total"},
         "setup": "orders fixture", "scan_id": "", "updated_at": _now()},
    ]


async def _run_test_gen(req: TestGenReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running",
                                     "summary": "generating test cases…"})
    await asyncio.sleep(0.4)
    cases = _fake_test_cases(req.app_id, req.context)
    for c in cases:
        c["scan_id"] = jid
    job.update(status="done", finished_at=_now(),
               summary=f"test-gen {req.app_id}; {len(cases)} cases")
    await _push("/api/change-jobs", {**job, "status": "done"})
    # push the list of cases
    await _push(f"/api/test-cases/{req.app_id}", {"app_id": req.app_id,
                 "cases": cases}, method="PUT")


def _fake_test_results(app_id: str, phase: str, target: str) -> List[Dict[str, Any]]:
    """Derive pass/fail per case from the phase. The post phase flips ONE case
    to fail so the compare step has a regression to surface."""
    cases = [c["name"] for c in _fake_test_cases(app_id, {})]
    results = []
    for name in cases:
        if phase == "post" and name == "regression_orders_total":
            results.append({"case": name, "status": "fail", "duration_ms": 120,
                            "response_summary": "500 INTERNAL",
                            "error": "NPE in OrderService.total()"})
        else:
            results.append({"case": name, "status": "pass", "duration_ms": 35,
                            "response_summary": "200 OK", "error": ""})
    return results


async def _run_test_run(req: TestRunReq, jid: str, run_id: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running",
                                     "summary": f"running tests (phase={req.phase})…"})
    await asyncio.sleep(0.5)
    results = _fake_test_results(req.app_id, req.phase, req.target)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    run = {"id": run_id, "app_id": req.app_id, "phase": req.phase,
           "target": req.target, "results": results,
           "passed": passed, "failed": failed, "errors": 0,
           "run_at": _now(), "scan_id": jid}
    job.update(status="done", finished_at=_now(),
               summary=f"test-run {req.app_id} ({req.phase}); "
                       f"{passed} pass / {failed} fail")
    await _push("/api/change-jobs", {**job, "status": "done"})
    await _push(f"/api/test-runs/{run_id}", run, method="PUT")


async def _run_test_compare(req: TestCompareReq, jid: str):
    job = _JOBS[jid]
    job["status"] = "running"
    await _push("/api/change-jobs", {**job, "status": "running",
                                     "summary": "comparing pre vs post…"})
    await asyncio.sleep(0.4)
    # derive the diff from the canned pre/post results: one regression
    diff = [
        {"case": "health", "pre_status": "pass", "post_status": "pass",
         "verdict": "pass", "detail": ""},
        {"case": "app_index_200", "pre_status": "pass", "post_status": "pass",
         "verdict": "pass", "detail": ""},
        {"case": "regression_orders_total", "pre_status": "pass",
         "post_status": "fail", "verdict": "regression",
         "detail": "500 NPE in OrderService.total() — new failure post-cutover"},
    ]
    regressions = sum(1 for d in diff if d["verdict"] in ("regression", "new_failure"))
    td = {"app_id": req.app_id, "pre_run_id": req.pre_run_id,
          "post_run_id": req.post_run_id, "diff": diff,
          "regressions": regressions, "scan_id": jid, "scanned_at": _now(),
          "summary": f"{regressions} regression(s) on cutover"}
    job.update(status="done", finished_at=_now(),
               summary=f"test-compare {req.app_id}; {regressions} regression(s)")
    await _push("/api/change-jobs", {**job, "status": "done"})
    await _push(f"/api/test-diffs/{req.app_id}", td, method="PUT")


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