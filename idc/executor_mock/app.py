"""Mock external agent executor — **pull mode**.

The executor exposes **nothing** to the network: idc-migrate cannot reach it,
so it cannot push triggers. Instead this mock **polls** idc-migrate's
``POST /api/executor/tasks/claim`` for work, runs the canned scan/comb/modify/
... logic, pushes results back over the unchanged ``/api/*`` feedback surface
(``PUT /api/code-profiles/{app_id}``, ``POST /api/change-jobs``, ...), and marks
the task complete via ``POST /api/executor/tasks/{id}/complete``. This is a
reference loop a real executor can copy.

Run: ``idc executor-mock`` (poll loop). Config: ``IDC_MOCK_CALLBACK``
(idc-migrate base, default http://localhost:8010), ``IDC_EXECUTOR_TOKEN``
(shared secret, must match idc-migrate).

The ``_fake_*`` functions are the canned answers (a real executor swaps them
for a real scanner + repo writer); the ``_run_*`` runners are the per-kind
workers; ``_handle`` dispatches a claimed task to its runner.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

CALLBACK = os.getenv("IDC_MOCK_CALLBACK", "http://localhost:8010").rstrip("/")
TOKEN = os.getenv("IDC_EXECUTOR_TOKEN", "")
POLL_INTERVAL = float(os.getenv("IDC_MOCK_POLL_INTERVAL", "2"))


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _jid() -> str:
    return f"cjob-{uuid.uuid4().hex[:10]}"


def _headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


async def _push(path: str, body: Dict[str, Any], method: str = "POST",
                url: Optional[str] = None):
    """Best-effort push to idc-migrate; never raise into the worker path.

    ``url`` (when set) is a full callback URL idc-migrate baked into the task
    payload — PUT/POST directly to it. When unset, fall back to the
    ``IDC_MOCK_CALLBACK`` base + ``path``."""
    import httpx  # lazy
    target = url or f"{CALLBACK}{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.request(method, target, json=body, headers=_headers())
            # httpx does NOT raise on 4xx/5xx by default — a 401 (token mismatch)
            # or 400/500 would otherwise silently drop the profile/results and the
            # job would look green while idc-migrate got nothing. Log non-2xx.
            if r.status_code >= 400:
                print(f"[mock] push {method} {target} -> HTTP {r.status_code}: "
                      f"{r.text[:200]}")
    except Exception as e:  # idc-migrate down? log, don't crash the worker
        print(f"[mock] push {method} {target} failed: {e!r}")


async def _post(path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST to an idc-migrate control endpoint (claim/complete/lease). Returns
    the parsed JSON or None on network/HTTP failure (logged, not raised)."""
    import httpx  # lazy
    target = f"{CALLBACK}{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(target, json=body, headers=_headers())
            if r.status_code >= 400:
                print(f"[mock] post {target} -> HTTP {r.status_code}: {r.text[:200]}")
                return None
            return r.json()
    except Exception as e:
        print(f"[mock] post {target} failed: {e!r}")
        return None


def _app_id_of(p: Dict[str, Any]) -> str:
    """The change-job's app_id is whichever identity the task carries."""
    return (p.get("app_id") or p.get("db_server_id") or p.get("server_id")
            or p.get("scope_id") or p.get("wave_id") or "")


def _job(tid: str, kind: str, p: Dict[str, Any], status: str, *,
         summary: str = "", patch_ref: str = "", error: str = "",
         finished_at: str = "") -> Dict[str, Any]:
    """Build a ChangeJob-shaped dict for the heartbeat/terminal push. The mock
    no longer keeps an in-memory _JOBS table (pull mode has no server); the job
    is reconstructed from the task payload + the status the worker is emitting."""
    return {"id": tid, "app_id": _app_id_of(p), "kind": kind,
            "repo_url": p.get("repo_url", ""), "branch": p.get("branch", ""),
            "status": status, "patch_ref": patch_ref, "summary": summary,
            "error": error, "created_at": p.get("created_at", _now()),
            "finished_at": finished_at}


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


def _fake_discover_repos(url: str) -> List[Dict[str, Any]]:
    """A plausible discovery: parse a git group/org url and synthesize the child
    repositories inside it. A real executor calls the SCM REST API (GitLab
    ``/groups/:id/projects?include_subgroups=true``, GitHub ``/orgs/:org/repos``)
    or walks ``git ls-remote`` refs with its own credentials; this mock just
    derives deterministic child urls from the group path so the discovery UI is
    demoable without SCM access. A single-repo url (``.git``) yields itself."""
    u = (url or "").strip().rstrip("/")
    base = u[:-4] if u.endswith(".git") else u
    name = base.rsplit("/", 1)[-1] if "/" in base else base
    # single-repo url -> just that repo (its own default branch)
    if u.endswith(".git"):
        return [{"url": u, "name": name, "branch": "main",
                 "description": f"{name} (single repository)",
                 "web_url": base}]
    # group/org url -> a deterministic set of child repos under it
    children = ["api", "web", "worker", "shared", "migrations"]
    return [{"url": f"{base}/{c}.git", "name": c, "branch": "main",
             "description": f"{name}/{c} service",
             "web_url": f"{base}/{c}"} for c in children]


# ---------------------------------------------------------------------------
# per-kind workers — take the task payload dict + task id, push results back,
# and return {summary, result_ref} so the poller can mark the task complete.
# ---------------------------------------------------------------------------
async def _run_scan(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    app_id = p.get("app_id", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "scan", p, "running",
                                         summary="scanning…"))
    await asyncio.sleep(0.5)  # pretend to work
    profile = _fake_profile(app_id, p.get("repo_url", ""), p.get("branch", ""), tid)
    summary = f"scanned {app_id}; 2 findings, pattern=replatform"
    await _push("/api/change-jobs", _job(tid, "scan", p, "done",
                                         summary=summary, finished_at=_now()))
    await _push(f"/api/code-profiles/{app_id}", profile, method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


async def _run_db_scan(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    db = p.get("db_server_id", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "db-scan", p, "running",
                                         summary="db-scanning…"))
    await asyncio.sleep(0.5)
    prof = _fake_db_profile(db, p.get("source_engine", ""), p.get("target_engine", ""), tid)
    if (p.get("mode") or "assess").lower() == "convert":
        prof["conversion"] = _fake_db_conversion(db, p.get("source_engine", ""),
                                                 p.get("target_engine", ""), tid)
    summary = (f"db-scan {db}; difficulty={prof['difficulty']}"
               + (" (+conversion)" if "conversion" in prof else ""))
    await _push("/api/change-jobs", _job(tid, "db-scan", p, "done",
                                         summary=summary, finished_at=_now()))
    await _push(f"/api/db-profiles/{db}", prof, method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


async def _run_modify(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    app_id = p.get("app_id", "")
    changes = p.get("changes") or []
    mode = p.get("mode", "plan")
    await _push("/api/change-jobs", _job(tid, "modify", p, "running", summary="modifying…"))
    await asyncio.sleep(0.5)

    # for any change the caller couldn't fully resolve (empty old/new), raise a
    # question — the operator answers, and a real executor would poll for it.
    unresolved = [c for c in changes if not (c.get("old") or "").strip()
                 or not (c.get("new") or "").strip()]
    for c in unresolved:
        ctx = {"category": c.get("category"), "kind": c.get("kind"),
               "file": c.get("file"), "line": c.get("line"),
               "old": c.get("old", ""), "new": c.get("new", ""),
               "title": c.get("title", ""), "evidence": c.get("evidence", "")}
        prompt = (f"What should {c.get('old') or 'this token'!r} in "
                  f"{c.get('file')}:{c.get('line') or '?'} ({c.get('category')}) become?")
        await _push(f"/api/apps/{app_id}/questions", {
            "job_id": tid, "kind": "choice", "prompt": prompt,
            "options": [c.get("new") or "${PLACEHOLDER}"], "context": ctx})

    patch = f"mock-patch-{tid}" if mode == "plan" else f"mock-pr-{tid}"
    summary = f"modify {mode}: {len(changes)} change(s), {len(unresolved)} question(s) raised"
    await _push("/api/change-jobs", _job(tid, "modify", p, "done",
                                         summary=summary, patch_ref=patch,
                                         finished_at=_now()))
    return {"summary": summary, "result_ref": patch}


async def _run_runtime_containerize(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    app_id = p.get("app_id", "")
    cb = p.get("callback") or None
    mode = p.get("mode", "plan")
    await _push("/api/change-jobs", _job(tid, "runtime-containerize", p, "running",
                                         summary="inferring scaffold from runtime…"))
    await asyncio.sleep(0.5)
    prof = _fake_runtime_profile(app_id, p.get("server_id", ""), tid)
    patch = f"dockerfile-scaffold-{tid}" if mode == "plan" else f"dockerfile-pr-{tid}"
    summary = f"runtime-containerize {mode}: inferred Dockerfile scaffold"
    await _push("/api/change-jobs", _job(tid, "runtime-containerize", p, "done",
                                         summary=summary, patch_ref=patch,
                                         finished_at=_now()))
    await _push(f"/api/code-profiles/{app_id}", prof, method="PUT", url=cb)
    return {"summary": summary, "result_ref": patch}


async def _run_legacy_disposition(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    sid = p.get("server_id", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "legacy-disposition", p, "running",
                                         summary="analyzing EOL workload…"))
    await asyncio.sleep(0.4)
    disp = _fake_legacy_disposition(sid, p.get("context") or {}, tid)
    summary = f"legacy-disposition {sid}; {disp['disposition']}"
    await _push("/api/change-jobs", _job(tid, "legacy-disposition", p, "done",
                                         summary=summary, finished_at=_now()))
    await _push(f"/api/legacy-dispositions/{sid}", disp, method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


async def _run_doc(p: Dict[str, Any], tid: str, doc_type: str, gen) -> Dict[str, Any]:
    wave_id = p.get("wave_id", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, doc_type, p, "running",
                                         summary=f"generating {doc_type}…"))
    await asyncio.sleep(0.4)
    md = gen(wave_id, p.get("context") or {})
    summary = f"{doc_type} for {wave_id}"
    await _push("/api/change-jobs", _job(tid, doc_type, p, "done",
                                         summary=summary, finished_at=_now()))
    slug = "as-built" if doc_type == "as_built" else doc_type
    await _push(f"/api/docs/{slug}/{wave_id}", {
        "doc_type": doc_type, "scope_id": wave_id, "doc_md": md,
        "scan_id": tid, "scanned_at": _now(),
        "summary": f"{doc_type} for wave {wave_id}",
    }, method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


async def _run_iac_emit(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    scope_id = p.get("scope_id", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "iac-emit", p, "running",
                                         summary="emitting IaC + guardrails…"))
    await asyncio.sleep(0.5)
    art = _fake_iac_artifact(p.get("scope", ""), scope_id, p.get("context") or {}, tid)
    summary = f"iac-emit {scope_id}; pass={art['guardrail_pass']}"
    await _push("/api/change-jobs", _job(tid, "iac-emit", p, "done",
                                         summary=summary, finished_at=_now()))
    await _push(f"/api/iac-artifacts/{scope_id}", art, method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


async def _run_postmig_optimize(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    sid = p.get("server_id", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "postmig-optimize", p, "running",
                                         summary="analyzing post-mig metrics…"))
    await asyncio.sleep(0.5)
    recs = _fake_postmig_recs(sid, p.get("context") or {}, tid)
    summary = f"postmig-optimize {sid}; {len(recs)} recs"
    await _push("/api/change-jobs", _job(tid, "postmig-optimize", p, "done",
                                         summary=summary, finished_at=_now()))
    for r in recs:
        await _push(f"/api/postmig-recs/{sid}/{r['kind']}", r, method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


async def _run_test_gen(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    app_id = p.get("app_id", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "test-gen", p, "running",
                                         summary="generating test cases…"))
    await asyncio.sleep(0.4)
    cases = _fake_test_cases(app_id, p.get("context") or {})
    for c in cases:
        c["scan_id"] = tid
    summary = f"test-gen {app_id}; {len(cases)} cases"
    await _push("/api/change-jobs", _job(tid, "test-gen", p, "done",
                                         summary=summary, finished_at=_now()))
    await _push(f"/api/test-cases/{app_id}", {"app_id": app_id, "cases": cases},
                method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


async def _run_test_run(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    app_id = p.get("app_id", "")
    phase = p.get("phase", "pre")
    target = p.get("target", "")
    run_id = p.get("run_id") or f"trun-{tid[5:]}"
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "test-run", p, "running",
                                         summary=f"running tests (phase={phase})…"))
    await asyncio.sleep(0.5)
    results = _fake_test_results(app_id, phase, target)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    run = {"id": run_id, "app_id": app_id, "phase": phase,
           "target": target, "results": results,
           "passed": passed, "failed": failed, "errors": 0,
           "run_at": _now(), "scan_id": tid}
    summary = f"test-run {app_id} ({phase}); {passed} pass / {failed} fail"
    await _push("/api/change-jobs", _job(tid, "test-run", p, "done",
                                         summary=summary, finished_at=_now()))
    await _push(f"/api/test-runs/{run_id}", run, method="PUT", url=cb)
    return {"summary": summary, "result_ref": run_id}


async def _run_test_compare(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    app_id = p.get("app_id", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "test-compare", p, "running",
                                         summary="comparing pre vs post…"))
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
    td = {"app_id": app_id, "pre_run_id": p.get("pre_run_id", ""),
          "post_run_id": p.get("post_run_id", ""), "diff": diff,
          "regressions": regressions, "scan_id": tid, "scanned_at": _now(),
          "summary": f"{regressions} regression(s) on cutover"}
    summary = f"test-compare {app_id}; {regressions} regression(s)"
    await _push("/api/change-jobs", _job(tid, "test-compare", p, "done",
                                         summary=summary, finished_at=_now()))
    await _push(f"/api/test-diffs/{app_id}", td, method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


async def _run_discover_repos(p: Dict[str, Any], tid: str) -> Dict[str, Any]:
    """Discover the repositories inside a git group/org url (the Code tab's
    "scan a git group" action). Pushes the discovered list back to
    ``PUT /api/repos/discover/{scan_id}/result``. The mock derives deterministic
    child repos; a real executor walks the SCM API / ls-remote with its creds."""
    scan_id = p.get("scan_id", "")
    url = p.get("url", "")
    cb = p.get("callback") or None
    await _push("/api/change-jobs", _job(tid, "discover-repos", p, "running",
                                         summary="discovering repositories…"))
    await asyncio.sleep(0.5)
    repos = _fake_discover_repos(url)
    summary = f"discover-repos {url}; {len(repos)} repo(s) found"
    await _push("/api/change-jobs", _job(tid, "discover-repos", p, "done",
                                         summary=summary, finished_at=_now()))
    await _push(f"/api/repos/discover/{scan_id}/result", {"repos": repos},
                method="PUT", url=cb)
    return {"summary": summary, "result_ref": ""}


# map task kind -> worker
_WORKERS = {
    "scan": _run_scan,
    "comb": _run_scan,           # comb produces a profile too (same shape)
    "modify": _run_modify,
    "db-scan": _run_db_scan,
    "runtime-containerize": _run_runtime_containerize,
    "legacy-disposition": _run_legacy_disposition,
    "cutover-playbook": lambda p, tid: _run_doc(p, tid, "cutover", _fake_cutover_playbook),
    "as-built": lambda p, tid: _run_doc(p, tid, "as_built", _fake_as_built),
    "iac-emit": _run_iac_emit,
    "postmig-optimize": _run_postmig_optimize,
    "test-gen": _run_test_gen,
    "test-run": _run_test_run,
    "test-compare": _run_test_compare,
    "discover-repos": _run_discover_repos,
}


async def _handle(task: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a claimed task to its per-kind worker. Returns the worker's
    ``{summary, result_ref}`` so the poller can mark the task complete."""
    kind = task.get("kind", "")
    worker = _WORKERS.get(kind)
    p = task.get("payload") or {}
    p = dict(p)
    p.setdefault("created_at", task.get("created_at", _now()))
    if worker is None:
        raise ValueError(f"mock has no worker for task kind {kind!r}")
    return await worker(p, task["task_id"])


async def _complete(tid: str, status: str, *, summary: str = "",
                    result_ref: str = "", error: str = "") -> None:
    await _post(f"/api/executor/tasks/{tid}/complete", {
        "status": status, "summary": summary, "result_ref": result_ref,
        "error": error})


async def poll_once() -> bool:
    """Claim + run one task. Returns True if a task was handled, False if idle.
    On a worker error the task is marked ``error`` (never left claimed)."""
    task = await _post("/api/executor/tasks/claim", {})
    if not task or not task.get("task_id"):
        return False
    tid = task["task_id"]
    try:
        out = await _handle(task)
        await _complete(tid, "done", summary=out.get("summary", ""),
                        result_ref=out.get("result_ref", ""))
    except Exception as e:  # never strand a claimed task
        print(f"[mock] task {tid} failed: {e!r}")
        await _complete(tid, "error", error=str(e))
    return True


async def run_forever() -> None:
    """The poll loop ``idc executor-mock`` runs: claim → handle → complete,
    sleeping ``IDC_MOCK_POLL_INTERVAL`` s when idle."""
    print(f"[mock] pulling tasks from {CALLBACK}/api/executor/tasks/claim "
          f"(token {'set' if TOKEN else 'NOT set'})")
    while True:
        try:
            handled = await poll_once()
        except Exception as e:  # a bug in the poller itself shouldn't kill it
            print(f"[mock] poll_once error: {e!r}")
            handled = False
        if not handled:
            await asyncio.sleep(POLL_INTERVAL)


def main():
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()