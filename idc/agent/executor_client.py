"""Client for the external agent executor (idc-migrate → executor direction).

The executor scans/combs/modifies application source repos and pushes results
back to idc-migrate (see ``docs/agent-executor.md``). This client issues the
request-side calls: trigger a scan/comb/modify job and poll its status. The
executor reports results back via the push endpoints
(``PUT /api/code-profiles/{app_id}``, ``POST /api/change-jobs``).

Uses httpx with a short connect timeout; the jobs themselves are async on the
executor side (responses are ``202 + job_id``). No hard dependency on httpx at
import time — it is imported lazily so the core stays importable without it.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..config import Settings


class ExecutorError(Exception):
    """Raised when the executor returns a non-2xx / non-202 response."""

    def __init__(self, status: int, body: str):
        super().__init__(f"executor HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class ExecutorClient:
    def __init__(self, settings: Settings):
        self.base = (settings.executor_url or "").rstrip("/")
        self.token = settings.executor_token
        self.timeout = settings.executor_timeout

    @property
    def configured(self) -> bool:
        return bool(self.base)

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str,
                 json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.configured:
            raise ExecutorError(0, "IDC_EXECUTOR_URL not configured")
        import httpx  # lazy import
        url = f"{self.base}{path}"
        try:
            r = httpx.request(method, url, json=json, headers=self._headers(),
                              timeout=self.timeout)
        except Exception as e:  # network / connect / timeout
            raise ExecutorError(0, f"request failed: {e!r}") from e
        body = r.text
        if r.status_code not in (200, 202):
            raise ExecutorError(r.status_code, body)
        if not body:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": body}

    # -- actions -----------------------------------------------------------
    def scan(self, app_id: str, repo_url: str, branch: str = "",
             callback: str = "") -> Dict[str, Any]:
        """Trigger a code scan. Returns ``{job_id, status}`` (async)."""
        return self._request("POST", "/v1/scan", {
            "app_id": app_id, "repo_url": repo_url, "branch": branch,
            "callback": callback})

    def comb(self, app_id: str, repo_url: str, branch: str = "",
             callback: str = "") -> Dict[str, Any]:
        """Trigger a comb (produce a CodeProfile). Returns ``{job_id, status}``."""
        return self._request("POST", "/v1/comb", {
            "app_id": app_id, "repo_url": repo_url, "branch": branch,
            "callback": callback})

    def db_scan(self, db_server_id: str, source_engine: str = "",
                target_engine: str = "", callback: str = "",
                mode: str = "assess") -> Dict[str, Any]:
        """Trigger a heterogeneous DB-scan (F5/F6). ``db_server_id`` is the DB
        host's stable identity (hostname). The executor scans the DB schema/SQL
        and pushes a ``DBConversionProfile`` back to
        ``PUT /api/db-profiles/{db_server_id}``.

        ``mode``:
          * ``assess`` (default) — grade-only (A/B/C + man-days + blockers),
            back-compat with existing executors.
          * ``convert`` — also emit the converted DDL + a per-object
            compatibility report in ``DBConversionProfile.conversion`` (F6).
        Returns ``{job_id, status}``."""
        return self._request("POST", "/v1/db-scan", {
            "db_server_id": db_server_id, "source_engine": source_engine,
            "target_engine": target_engine, "mode": mode, "callback": callback})

    def runtime_containerize(self, app_id: str, server_id: str,
                             inventory: Optional[dict] = None,
                             mode: str = "plan", callback: str = "") -> Dict[str, Any]:
        """Trigger the no-source containerization path (F9). ``inventory`` is
        the Zabbix/Prometheus-discovered process + port + software inventory
        for the app's host (no repo_url needed). The executor infers a
        Dockerfile scaffold + a ``CodeProfile`` tagged ``source=runtime-derived``
        and pushes the profile back to ``PUT /api/code-profiles/{app_id}``.
        ``mode=plan`` (default) is dry-run scaffold; ``execute`` writes it."""
        return self._request("POST", "/v1/runtime-containerize", {
            "app_id": app_id, "server_id": server_id,
            "inventory": inventory or {}, "mode": mode, "callback": callback})

    def modify(self, app_id: str, repo_url: str, branch: str = "",
               mode: str = "plan", scope: Optional[list] = None,
               changes: Optional[list] = None,
               notes: Optional[list] = None) -> Dict[str, Any]:
        """Trigger a code modification. ``mode`` = plan (dry-run) | execute.

        ``changes`` is the concrete change list (built by
        ``codeintel.build_change_spec``) — one item per change with file/line/
        old/new so the executor doesn't have to re-scan or guess what to change.
        ``scope`` is kept as a category filter for back-compat, but ``changes``
        is the authoritative instruction set; when present it is already
        scope-filtered.
        """
        body: Dict[str, Any] = {
            "app_id": app_id, "repo_url": repo_url, "branch": branch,
            "mode": mode, "scope": scope or []}
        if changes is not None:
            body["changes"] = changes
        if notes:
            body["notes"] = notes
        return self._request("POST", "/v1/modify", body)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Poll a job's status on the executor."""
        return self._request("GET", f"/v1/jobs/{job_id}")

    def legacy_disposition(self, server_id: str, context: dict,
                           callback: str = "") -> Dict[str, Any]:
        """Trigger a legacy/unsupported-OS disposition analysis (F7).

        ``context`` is the workload shape the executor needs to choose
        containerize vs re-platform vs rewrite vs retain: the host's OS + EOL
        bucket, runtime, role, criticality, whether a source repo exists, the
        runtime inventory (process/port/software), and the code-profile summary.
        The executor pushes a ``LegacyDisposition`` back to
        ``PUT /api/legacy-dispositions/{server_id}``. Returns ``{job_id, status}``.
        """
        return self._request("POST", "/v1/legacy-disposition", {
            "server_id": server_id, "context": context, "callback": callback})

    def cutover_playbook(self, wave_id: str, context: dict,
                         callback: str = "") -> Dict[str, Any]:
        """Trigger a per-wave cutover-playbook generation (F9). ``context`` is
        the wave's members (server/role/target/ports/deps), the downtime window,
        and the deterministic risk basis. The executor pushes a ``DocArtifact``
        (markdown) back to ``PUT /api/docs/cutover/{wave_id}``. Returns
        ``{job_id, status}``."""
        return self._request("POST", "/v1/cutover-playbook", {
            "wave_id": wave_id, "context": context, "callback": callback})

    def as_built(self, wave_id: str, context: dict,
                 callback: str = "") -> Dict[str, Any]:
        """Trigger a per-wave as-built doc generation (F9). ``context`` is the
        executed wave's stage_history, gate results, change jobs, and targets.
        The executor pushes a ``DocArtifact`` (markdown) back to
        ``PUT /api/docs/as-built/{wave_id}``. Returns ``{job_id, status}``."""
        return self._request("POST", "/v1/as-built", {
            "wave_id": wave_id, "context": context, "callback": callback})

    def iac_emit(self, scope: str, scope_id: str, context: dict,
                 callback: str = "") -> Dict[str, Any]:
        """Trigger IaC generation + Well-Architected guardrail checks (F5).

        ``scope`` is ``landing_zone`` (``scope_id`` = ``lz:<archetype>``, with
        the blueprint) or ``workload`` (``scope_id`` = ``wl:<server_id>``, with
        the match). The executor emits Terraform modules + runs the guardrail
        checks and pushes an ``IaCArtifact`` back to
        ``PUT /api/iac-artifacts/{scope_id}``. Returns ``{job_id, status}``.
        """
        return self._request("POST", "/v1/iac-emit", {
            "scope": scope, "scope_id": scope_id, "context": context,
            "callback": callback})

    def postmig_optimize(self, server_id: str, context: dict,
                         callback: str = "") -> Dict[str, Any]:
        """Trigger post-migration optimization analysis (F10). ``context`` is
        the host's cloud target + spec + a metrics window (or idc-migrate
        forwards Prometheus data). The executor pulls post-mig cloud metrics,
        detects anomalies, and pushes ``PostMigRecommendation`` records back to
        ``PUT /api/postmig-recs/{server_id}/{kind}`` (one per kind). Returns
        ``{job_id, status}``.
        """
        return self._request("POST", "/v1/postmig-optimize", {
            "server_id": server_id, "context": context, "callback": callback})

    def test_gen(self, app_id: str, context: dict,
                 callback: str = "") -> Dict[str, Any]:
        """Trigger test-case generation (F11). ``context`` is the app's
        CodeProfile + workload shape. The executor pushes TestCase records back
        to ``PUT /api/test-cases/{app_id}`` (a list). Returns ``{job_id, status}``.
        """
        return self._request("POST", "/v1/test-gen", {
            "app_id": app_id, "context": context, "callback": callback})

    def test_run(self, app_id: str, phase: str, target: str, context: dict,
                 callback: str = "") -> Dict[str, Any]:
        """Trigger a test run (F11). ``phase`` = pre (baseline before cutover)
        or post (after cutover); ``target`` is the endpoint base to hit. The
        executor pushes a TestRun back to ``PUT /api/test-runs/{run_id}``.
        Returns ``{job_id, status}``.
        """
        return self._request("POST", "/v1/test-run", {
            "app_id": app_id, "phase": phase, "target": target,
            "context": context, "callback": callback})

    def test_compare(self, app_id: str, pre_run_id: str, post_run_id: str,
                     callback: str = "") -> Dict[str, Any]:
        """Trigger a pre-vs-post test comparison (F11). The executor pushes a
        TestDiff back to ``PUT /api/test-diffs/{app_id}``. Returns
        ``{job_id, status}``.
        """
        return self._request("POST", "/v1/test-compare", {
            "app_id": app_id, "pre_run_id": pre_run_id,
            "post_run_id": post_run_id, "callback": callback})


def get_executor_client(settings: Settings) -> ExecutorClient:
    return ExecutorClient(settings)


def executor_status(settings: Settings) -> Dict[str, Any]:
    """Probe the external executor's connectivity for `doctor` / the web status
    indicator. Returns:
      {configured, enabled, url, token_set, reachable, status_code, detail, version}

    ``reachable`` is True only when ``GET {url}/health`` returns 200 (executors
    should expose ``/health`` per the contract). A connection error -> reachable
    False + detail; a non-200 HTTP response -> reachable False with the code
    (base responds but no healthy /health). Never raises."""
    base = (settings.executor_url or "").rstrip("/")
    out: Dict[str, Any] = {
        "configured": bool(base), "enabled": settings.executor_enabled,
        "url": base or None, "token_set": bool(settings.executor_token),
        "reachable": False, "status_code": None, "detail": None, "version": None,
    }
    if not base:
        out["detail"] = "IDC_EXECUTOR_URL not configured"
        return out
    if not settings.executor_enabled:
        out["detail"] = "executor disabled (IDC_EXECUTOR_ENABLED=false)"
        # still probe so the indicator can say "disabled but host up"
    import httpx
    try:
        r = httpx.get(f"{base}/health", timeout=min(settings.executor_timeout, 5))
        out["status_code"] = r.status_code
        if r.status_code == 200:
            out["reachable"] = True
            try:
                body = r.json()
                out["version"] = body.get("version")
                out["detail"] = body.get("service") or "ok"
            except Exception:
                out["detail"] = "ok"
        else:
            out["detail"] = f"/health returned {r.status_code} (no healthy endpoint?)"
    except Exception as e:
        out["detail"] = f"unreachable: {e!r}"
    return out