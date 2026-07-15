"""F6 — migration execution state machine + declarative validation gates.

A ``MigrationJob`` tracks ONE server's actual migration through a cutover-safe
state machine (planned -> replicating -> tested -> ready_for_cutover ->
cut_over -> finalized), with revert at every non-terminal stage and an
explicit rolled_back sink (see ``models.MIGRATION_JOB_TRANSITIONS``).

Phase 2 ships in **track-only mode**: the operator / an external tool performs
the migration and idc-migrate records state + runs declarative post-launch
validation gates. The Tencent SMS/DTS driver (F6.5) populates ``executor_ref``
when wired; until then ``executor_ref`` carries a free string for the external
runner.

Validation gates are declarative dicts ``{name, kind, must_pass, ...params}``.
Kinds idc-migrate can evaluate automatically (``connect``, ``http_response``,
``app_health_promql``) return ``pass``/``fail``; kinds that need an agent
(``process_up``, ``volume_consistency``) return ``pending`` — a ``must_pass``
pending gate blocks ``advance(..., finalized)`` until the operator resolves it
via ``complete`` (manual override) or marks the gate result.
"""
from __future__ import annotations

import socket
import contextvars
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import Settings
from .models import (
    MigrationJob, Server, Match,
    MIGRATION_JOB_STATUSES, MIGRATION_JOB_TRANSITIONS, MIGRATION_JOB_TERMINAL,
    MJS_PLANNED, MJS_REPLICATING, MJS_TESTED, MJS_READY_FOR_CUTOVER,
    MJS_CUT_OVER, MJS_FINALIZED, MJS_ROLLED_BACK,
    MJK_HOST, MJK_DB, MJK_CODE,
    _now,
)


class InvalidTransition(Exception):
    """Raised when a MigrationJob transition isn't in the allowed map."""

    def __init__(self, frm: str, to: str):
        super().__init__(f"invalid migration-job transition: {frm!r} -> {to!r}")
        self.frm, self.to = frm, to


class GatesNotSatisfied(Exception):
    """Raised when advancing to finalized with unsatisfied must_pass gates."""

    def __init__(self, pending: List[Dict[str, Any]]):
        super().__init__(f"cannot finalize: {len(pending)} must_pass gate(s) not satisfied: "
                         + ", ".join(g.get("name", "?") for g in pending))
        self.pending = pending


# F11 — contextvar hook so the test_regression gate can read a TestDiff off the
# store without the gate signature carrying a store (the other evaluators are
# store-free). ``run_validation_gates`` sets this when a store is passed.
_TEST_DIFF_GETTER: contextvars.ContextVar = contextvars.ContextVar(
    "_idc_test_diff_getter", default=None)
# F6 — same pattern for the db_conversion_blocked gate (reads the stored
# DBConversionProfile's blocked-object count for the db host).
_DB_CONV_GETTER: contextvars.ContextVar = contextvars.ContextVar(
    "_idc_db_conv_getter", default=None)


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------
def _transition(job: MigrationJob, to_status: str, store=None, by: str = "system",
                note: str = "", check_gates: bool = False) -> MigrationJob:
    allowed = MIGRATION_JOB_TRANSITIONS.get(job.status, ())
    if to_status not in allowed:
        raise InvalidTransition(job.status, to_status)
    if check_gates and to_status == MJS_FINALIZED:
        pending = [g for g in job.validation_gates
                   if g.get("must_pass") and g.get("result") not in ("pass", "skipped")]
        if pending:
            raise GatesNotSatisfied(pending)
    job.stage_history.append({"status": to_status, "at": _now(), "by": by,
                              "note": note, "from": job.status})
    job.status = to_status
    job.updated_at = _now()
    if store is not None:
        store.upsert_migration_job(job)
    return job


def advance(job: MigrationJob, to_status: str, store=None, by: str = "system",
            note: str = "") -> MigrationJob:
    """Forward transition (validated against MIGRATION_JOB_TRANSITIONS).

    Advancing to ``finalized`` checks that every ``must_pass`` gate is
    satisfied (pass/skipped) — use ``complete`` for a manual override."""
    return _transition(job, to_status, store=store, by=by, note=note, check_gates=True)


def revert(job: MigrationJob, to_status: str, store=None, by: str = "system",
           note: str = "") -> MigrationJob:
    """Revert to an earlier stage (revert edges live in the same map)."""
    return _transition(job, to_status, store=store, by=by,
                       note=("revert: " + note) if note else "revert")


def complete(job: MigrationJob, store=None, by: str = "operator",
             note: str = "") -> MigrationJob:
    """Manual mark-complete: operator confirms the migration is done and all
    gates are satisfied. Sets finalized + marks pending must_pass gates as
    ``skipped`` (operator-confirmed) — a deliberate override of the gate check
    so external-tool migrations don't get stuck on gates idc can't auto-evaluate.
    """
    if job.status in MIGRATION_JOB_TERMINAL:
        raise InvalidTransition(job.status, MJS_FINALIZED)
    for g in job.validation_gates:
        if g.get("must_pass") and g.get("result") not in ("pass", "fail", "skipped"):
            g["result"] = "skipped"
            g["result_by"] = by
            g["result_at"] = _now()
    note = note or "manual mark-complete (operator-confirmed)"
    # complete allows finalized from any non-terminal state (operator override)
    if MJS_FINALIZED not in MIGRATION_JOB_TRANSITIONS.get(job.status, ()):
        # force the terminal transition by recording it directly
        job.stage_history.append({"status": MJS_FINALIZED, "at": _now(), "by": by,
                                   "note": "override: " + note, "from": job.status})
        job.status = MJS_FINALIZED
        job.updated_at = _now()
        if store is not None:
            store.upsert_migration_job(job)
        return job
    return _transition(job, MJS_FINALIZED, store=store, by=by, note=note, check_gates=False)


# ---------------------------------------------------------------------------
# validation gates
# ---------------------------------------------------------------------------
def _gate_connect(params: Dict[str, Any], settings: Settings) -> Tuple[str, str]:
    host = str(params.get("host") or "")
    port = int(params.get("port") or 0)
    timeout = float(params.get("timeout") or 5)
    if not host or not port:
        return "pending", "missing host/port"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "pass", f"tcp {host}:{port} reachable"
    except Exception as e:
        return "fail", f"tcp {host}:{port} unreachable: {e}"


def _gate_http(params: Dict[str, Any], settings: Settings) -> Tuple[str, str]:
    url = str(params.get("url") or "")
    expect = int(params.get("expect_status") or 200)
    timeout = float(params.get("timeout") or 10)
    if not url:
        return "pending", "missing url"
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        return ("pass" if r.status_code == expect else "fail",
                f"HTTP {r.status_code} (expected {expect})")
    except Exception as e:
        return "fail", f"HTTP request failed: {e}"


def _gate_promql(params: Dict[str, Any], settings: Settings) -> Tuple[str, str]:
    """Query Prometheus for a health metric; pass if the vector is non-empty
    (or matches an optional ``min_value``). Needs IDC_PROM_URL."""
    metric = str(params.get("metric") or "")
    if not settings.has_prometheus() or not metric:
        return "pending", "prometheus not configured / no metric"
    try:
        import httpx
        r = httpx.get(f"{settings.prom_url}/api/v1/query",
                      params={"query": metric}, timeout=settings.prom_timeout)
        if r.status_code != 200:
            return "fail", f"promql HTTP {r.status_code}"
        res = (r.json().get("data") or {}).get("result") or []
        if not res:
            return "fail", f"promql {metric!r} returned no series"
        min_value = params.get("min_value")
        if min_value is not None:
            try:
                val = float(res[0]["value"][1])
                return ("pass" if val >= float(min_value) else "fail",
                        f"promql {metric} = {val} (min {min_value})")
            except Exception:
                return "fail", f"promql {metric} value parse failed"
        return "pass", f"promql {metric!r} returned {len(res)} series"
    except Exception as e:
        return "fail", f"promql error: {e}"


# manual gates — idc-migrate cannot evaluate without an agent; stay pending
def _gate_manual(params: Dict[str, Any], settings: Settings) -> Tuple[str, str]:
    return "pending", "requires operator/agent confirmation"


def _gate_test_regression(params: Dict[str, Any], settings: Settings) -> Tuple[str, str]:
    """F11 — the real version of the manual ``process_up`` stub: read the app's
    latest TestDiff and fail when there's a regression / new_failure. The diff
    is produced by the executor's test-compare step (pre-run vs post-run).

    ``params`` carries ``app_id`` (the app under test). A store is NOT passed
    here (the gate signature is store-free for the other evaluators); the
    evaluator reads the diff off a module-level hook set by ``run_validation_gates``
    so it stays testable without a global.

    Verdict:
      * regressions > 0 → ``fail`` (blocks finalize — the whole point).
      * a clean diff exists → ``pass``.
      * no diff yet (testing not set up, or not compared) → ``pass``, NOT
        ``pending``. A pending must_pass gate would lock out EVERY host
        finalize on estates that never opted into automated testing — too
        aggressive a default. The gate only blocks when testing was actually
        run AND found a regression; opting in is the operator's choice.
    """
    app_id = str(params.get("app_id") or "")
    getter = _TEST_DIFF_GETTER.get()
    if not app_id or getter is None:
        return "pass", "automated testing not configured for this app"
    try:
        diff = getter(app_id)
    except Exception:
        # store lookup failed (DB error) — fail CLOSED on a must_pass cutover gate
        # rather than letting a transient outage evaluate as a clean "pass".
        return "pending", "test-diff lookup failed (store error) — not safe to pass"
    if diff is None:
        return "pass", "no test diff yet (testing not run for this app)"
    n = int(getattr(diff, "regressions", 0) or 0)
    if n > 0:
        return "fail", f"{n} regression(s) / new failure(s) in test diff"
    return "pass", f"test diff clean ({len(diff.diff)} case(s) compared)"


def _gate_db_conversion_blocked(params: Dict[str, Any], settings: Settings) -> Tuple[str, str]:
    """F6 — block a db-kind finalize when the host's conversion has objects with
    no engine equivalent (``status=blocked``). Those need a rewrite or operator
    ack before the DB is safe to cut over. Reads the DBConversionProfile off
    the store via the ``_DB_CONV_GETTER`` hook (set by run_validation_gates).

    Verdict (mirrors test_regression — no lockout for estates not using it):
      * blocked objects > 0 → ``fail`` (blocks finalize).
      * conversion exists, no blocked → ``pass``.
      * no conversion assessed → ``pass`` (don't block un-assessed DBs).
    """
    host = str(params.get("db_server_id") or params.get("host") or "")
    getter = _DB_CONV_GETTER.get()
    if not host or getter is None:
        return "pass", "DB conversion not assessed"
    try:
        profile = getter(host)
    except Exception:
        return "pending", "DB-conversion lookup failed (store error) — not safe to pass"
    if profile is None or not getattr(profile, "conversion", None):
        return "pass", "DB conversion not assessed (assess mode only)"
    blocked = sum(1 for o in (profile.conversion.objects or [])
                  if o.status == "blocked")
    if blocked > 0:
        return "fail", f"{blocked} blocked DB object(s) — rewrite or ack before cutover"
    return "pass", f"DB conversion clean ({len(profile.conversion.objects)} object(s))"


_GATE_EVALUATORS = {
    "connect": _gate_connect,
    "http_response": _gate_http,
    "app_health_promql": _gate_promql,
    "process_up": _gate_manual,
    "volume_consistency": _gate_manual,
    "test_regression": _gate_test_regression,
    "db_conversion_blocked": _gate_db_conversion_blocked,
}


def default_gates_for(kind: str, server: Optional[Server] = None,
                      match: Optional[Match] = None) -> List[Dict[str, Any]]:
    """A sensible default gate set per migration kind. The operator can edit
    these on the job before validate."""
    host = (server.ips[0] if server and server.ips else "") or (server.hostname if server else "")
    port = 0
    if match and match.target:
        port = int(match.target.extras.get("port") or 0)
    gates: List[Dict[str, Any]] = []
    if kind in (MJK_HOST, MJK_DB):
        if host:
            gates.append({"name": "ssh-or-service-up", "kind": "connect", "must_pass": True,
                          "host": host, "port": port or 22, "timeout": 5})
    if kind == MJK_HOST:
        gates.append({"name": "disk-consistency", "kind": "volume_consistency",
                      "must_pass": True})
    if kind == MJK_DB:
        gates.append({"name": "db-reachable", "kind": "connect", "must_pass": True,
                      "host": host, "port": port or 3306, "timeout": 5})
        # F6 — block finalize when a convert-mode profile has objects with no
        # engine equivalent (rewrite/ack first). must_pass so a blocked DB
        # actually stops the cutover; passes (no lockout) when not assessed.
        gates.append({"name": "db-conversion-blocked", "kind": "db_conversion_blocked",
                       "must_pass": True, "db_server_id": (server.hostname if server else host)})
    gates.append({"name": "app-health", "kind": "app_health_promql", "must_pass": False,
                  "metric": f"up{{instance=\"{_promql_label(host)}\"}}"})
    # F11 — the real version of the manual process_up stub: block finalize on a
    # regression in the pre-vs-post test diff. must_pass so a regression actually
    # stops the cutover. The gate reads the diff via the store hook in
    # run_validation_gates; with no store it stays pending (blocks) until the
    # comparison lands. app_id is filled by launch_wave from the job's app_id.
    if kind == MJK_HOST and getattr(server, "app_ids", None):
        gates.append({"name": "test-regression", "kind": "test_regression",
                      "must_pass": True, "app_id": (server.app_ids or [""])[0]})
    return gates


def _promql_label(value: str) -> str:
    """Escape a host/ip for a PromQL ``instance="..."`` label matcher.

    A hostname containing ``"`` / ``\\`` / a newline would otherwise break the
    query or match unintended series, yielding a false pass/fail on a cutover
    gate. Per PromQL, escape backslash then the quote, and \\n for newline.
    """
    v = value or ""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def run_validation_gates(job: MigrationJob, settings: Settings,
                         gates: Optional[List[Dict[str, Any]]] = None,
                         store: Optional[Any] = None
                         ) -> Tuple[bool, List[Dict[str, Any]]]:
    """Evaluate the job's validation gates. Returns (all_must_pass_ok, results).

    Updates each gate dict in place with ``result``/``detail``/``at``. A gate
    is ``ok`` for finalize purposes only if it is not ``must_pass`` or its
    result is ``pass``/``skipped``. Manual gates (``process_up``,
    ``volume_consistency``) stay ``pending`` — the operator resolves them via
    ``complete`` or by setting ``result`` manually.

    ``store`` (F11) — when passed, the ``test_regression`` gate can read the
    app's latest TestDiff off the store (via a contextvar hook so the gate
    evaluator signature stays store-free). When None, the test_regression gate
    stays ``pending`` (no diff to compare).
    """
    glist = gates if gates is not None else job.validation_gates
    token = None
    token_db = None
    if store is not None and hasattr(store, "get_test_diff"):
        def _get(app_id):
            # Let store errors propagate — the evaluator catches them and returns
            # ``pending`` (fail-closed). The old swallow->None made a transient DB
            # outage during cutover evaluate as ``pass`` on a must_pass gate.
            return store.get_test_diff(app_id)
        token = _TEST_DIFF_GETTER.set(_get)
    if store is not None and hasattr(store, "get_db_profile"):
        def _get_db(host):
            return store.get_db_profile(host)
        token_db = _DB_CONV_GETTER.set(_get_db)
    try:
        for g in glist:
            kind = g.get("kind") or "manual"
            evaluator = _GATE_EVALUATORS.get(kind, _gate_manual)
            result, detail = evaluator(g, settings)
            g["result"] = result
            g["detail"] = detail
            g["at"] = _now()
    finally:
        if token is not None:
            _TEST_DIFF_GETTER.reset(token)
        if token_db is not None:
            _DB_CONV_GETTER.reset(token_db)
    must_pass = [g for g in glist if g.get("must_pass")]
    all_ok = all(g.get("result") in ("pass", "skipped") for g in must_pass)
    return all_ok, glist


# ---------------------------------------------------------------------------
# wave launch
# ---------------------------------------------------------------------------
class LZNotReady(Exception):
    """Raised when ``launch_wave`` is called with ``enforce_lz_gate=True`` and a
    blocking archetype's LZ isn't finalized (F8 placement gate), or the wave
    contains undetermined (pending) hosts that have no LZ to land in."""

    def __init__(self, blocking: List[str], reason: str = ""):
        msg = reason or f"LZ not ready for archetypes: {', '.join(blocking)}"
        super().__init__(msg)
        self.blocking = blocking
        self.reason = reason


def launch_wave(store, wave_id: str, servers: List[Server],
                matches: List[Match], workloads: List[Any],
                kind: str = MJK_HOST, by: str = "operator",
                enforce_lz_gate: bool = False) -> List[MigrationJob]:
    """Create a MigrationJob (status=planned) for each server in the wave,
    with default validation gates from its match. Persists each job.

    F8 — ``enforce_lz_gate``: when True, refuse to launch a workload wave whose
    target LZ archetype isn't finalized (raises ``LZNotReady``). When False
    (default, track-only mode), the gate is evaluated and surfaced on the job's
    stage_history as a warning, but launch proceeds — the operator decides.
    """
    from .lz import check_lz_gate
    srv_by_id = {s.id: s for s in servers}
    match_by_id = {m.server_id: m for m in matches}
    # map server -> app_id via workloads
    srv_to_app: Dict[str, str] = {}
    for w in workloads:
        for sid in (w.server_ids or []):
            srv_to_app.setdefault(sid, w.app_id)
    # wave members
    wave = _get_wave(store, wave_id)
    if wave is None:
        raise ValueError(f"wave {wave_id} not found")
    # F8 — placement gate (LZ archetype readiness)
    gate = check_lz_gate(store, wave_id, servers, matches)
    if not gate["ok"] and enforce_lz_gate:
        raise LZNotReady(gate["blocking_archetypes"], gate["reason"])
    gate_note = "" if gate["ok"] else f"LZ gate warning: {gate['reason']}"
    # idempotency: a server that already has a non-terminal job for this wave
    # keeps its existing job — re-launching must not create duplicate jobs
    # (the old code inserted a fresh row per call, accumulating orphans and
    # duplicating handoff-CSV rows).
    existing = {j.server_id: j for j in store.list_migration_jobs(wave_id=wave_id)
                if j.status not in MIGRATION_JOB_TERMINAL}
    out: List[MigrationJob] = []
    for sid in (wave.server_ids or []):
        s = srv_by_id.get(sid)
        if not s:
            continue
        if sid in existing:
            out.append(existing[sid])
            continue
        m = match_by_id.get(sid)
        gates = default_gates_for(kind, server=s, match=m)
        history = [{"status": MJS_PLANNED, "at": _now(), "by": by, "note": "launched"}]
        if gate_note:
            history.append({"status": MJS_PLANNED, "at": _now(), "by": "system",
                            "note": gate_note, "from": MJS_PLANNED})
        job = MigrationJob(
            server_id=sid, app_id=srv_to_app.get(sid, ""), wave_id=wave_id,
            kind=kind, status=MJS_PLANNED, validation_gates=gates,
            stage_history=history,
        )
        store.upsert_migration_job(job)
        out.append(job)
    return out


def _get_wave(store, wave_id: str):
    for w in store.list_waves():
        if w.id == wave_id:
            return w
    return None


# ---------------------------------------------------------------------------
# F7 — wave execution rollup + handoff manifest
# ---------------------------------------------------------------------------
_NOT_LAUNCHED = "not_launched"


def _dominant_status(status_counts: Dict[str, int]) -> str:
    """Coarse wave-level progress label for the dashboard (F7):

      * ``done``         — every server finalized
      * ``rolled-back``  — every server rolled_back
      * ``running``       — any server past ``planned`` (replicating/tested/
                            ready_for_cutover/cut_over/finalized)
      * ``planned``       — all servers still planned / not yet launched

    The fine per-status breakdown is in ``status_counts``; this label is the
    one-glance rollup for the wave execution dashboard.
    """
    total = sum(status_counts.values())
    if not total:
        return "planned"
    if status_counts.get(MJS_FINALIZED, 0) == total:
        return "done"
    if status_counts.get(MJS_ROLLED_BACK, 0) == total:
        return "rolled-back"
    # every server reached a terminal state, but a MIX of finalized + rolled-back:
    # the wave is finished (no migration in flight), so report ``done`` rather than
    # the misleading ``running``. ``finalized`` is terminal, NOT in-progress —
    # counting it below made a fully-finished wave render as "running" forever.
    terminal = (status_counts.get(MJS_FINALIZED, 0)
                + status_counts.get(MJS_ROLLED_BACK, 0))
    if terminal == total:
        return "done"
    in_progress = sum(status_counts.get(s, 0) for s in
                      (MJS_REPLICATING, MJS_TESTED, MJS_READY_FOR_CUTOVER,
                       MJS_CUT_OVER))
    return "running" if in_progress > 0 else "planned"


def wave_execution_status(store, wave_id: str) -> Dict[str, Any]:
    """Aggregate a wave's MigrationJobs (+ ChangeJobs) into a rollup (F7).

    Returns ``{wave_id, wave_name, stage, server_count, status_counts,
    dominant_status, per_server}``. ``per_server`` lists each member server
    with its match target + migration-job state. ``dominant_status`` is the
    bottleneck (least-advanced non-rolled-back server) so an operator sees at a
    glance whether the wave is planned / running / done.
    """
    wave = _get_wave(store, wave_id)
    if not wave:
        return {"wave_id": wave_id, "error": "wave not found"}
    from .lz import archetype_for
    mjobs = store.list_migration_jobs(wave_id=wave_id)
    mjob_by_server = {j.server_id: j for j in mjobs}
    wave_ids = set(wave.server_ids or [])
    servers = {s.id: s for s in store.list_all_servers() if s.id in wave_ids}
    matches = {m.server_id: m for m in store.list_matches()}

    status_counts: Dict[str, int] = {s: 0 for s in MIGRATION_JOB_STATUSES}
    status_counts[_NOT_LAUNCHED] = 0
    per_server: List[Dict[str, Any]] = []
    for sid in (wave.server_ids or []):
        s = servers.get(sid)
        mj = mjob_by_server.get(sid)
        st = mj.status if mj else _NOT_LAUNCHED
        status_counts[st] = status_counts.get(st, 0) + 1
        m = matches.get(sid)
        per_server.append({
            "server_id": sid,
            "hostname": s.hostname if s else "",
            "app_ids": list(s.app_ids) if s else [],
            "target": (m.target.product if m and m.target else "") or "",
            "target_spec": (m.target.spec if m and m.target else "") or "",
            "region": (m.target.region if m and m.target else "") or "",
            "lz_archetype": archetype_for(s, m) if s else "",   # F8 placement
            "migration_job_id": mj.id if mj else "",
            "kind": mj.kind if mj else "",
            "executor_ref": mj.executor_ref if mj else "",
            "status": st,
        })
    return {
        "wave_id": wave_id, "wave_name": wave.name, "wave_stage": wave.stage,
        "server_count": len(wave.server_ids or []),
        "status_counts": status_counts,
        "dominant_status": _dominant_status(status_counts),
        "per_server": per_server,
    }


def handoff_rows(store, wave_id: str) -> List[Dict[str, Any]]:
    """The assessment→execution handoff manifest (F7): one row per server in
    the wave, with target + tool + status — importable into the downstream
    runner (Tencent SMS / executor trigger). Mirrors the AWS Migration
    Hub→MGN CSV handoff."""
    roll = wave_execution_status(store, wave_id)
    return [{
        "wave_id": roll["wave_id"],
        "server_id": r["server_id"],
        "hostname": r["hostname"],
        "app_id": (r["app_ids"][0] if r["app_ids"] else ""),
        "app_ids": ";".join(r["app_ids"]),
        "target_product": r["target"],
        "target_spec": r["target_spec"],
        "region": r["region"],
        "tool": r["kind"],
        "executor_ref": r["executor_ref"],
        "status": r["status"],
        "migration_job_id": r["migration_job_id"],
    } for r in roll.get("per_server", [])]