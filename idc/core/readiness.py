"""F10 — portfolio readiness heatmap (per-wave "is it safe to cut?" scorecard).

Collapses the scattered migration signals into one green/yellow/red heatmap per
wave, à la AWS CAF MRA / Azure CAF "is this wave ready to cutover?". The score is
deterministic — every signal reads from persisted state (no LLM) — so the
"safe to cut?" answer is reproducible/auditable and the LLM only narrates it.

Six signals per wave (each green/yellow/red):
  * ``lz_ready``            — F8: every target archetype's LZ is finalized.
  * ``db_conversion``       — F5: db hosts assessed + no grade-C blocker outstanding.
  * ``code_refactor``       — executor: apps profiled, no blockers, a done ChangeJob.
  * ``deps_resolved``       — F1 + DAG: depends_on waves are done; no orphan code dep.
  * ``cutover_rehearsal``   — F6: every member MigrationJob reached ``tested``+.
  * ``rollback_channel``   — F5: db jobs have reverse_replication on (rollback net).
  * ``hw_support``          — data-gap: member hardware under warranty through
                              cutover (expired→red / expiring→yellow / active→green
                              / unknown→n/a; n/a is ignored by the rollup).
  * ``os_support``          — data-gap: member OS under vendor support through
                              cutover (same levels; expired OS = silent-rehost risk).

Wave rollup = worst case (red blocks cutover). A wave with no MigrationJobs
yet is "not launched" (red on cutover_rehearsal, n/a elsewhere).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .codeintel import index_profiles
from .match import (
    WARRANTY_ACTIVE, WARRANTY_EXPIRING, WARRANTY_EXPIRED, WARRANTY_UNKNOWN,
    WARRANTY_EXPIRING_DAYS,
)
from .models import (
    ChangeJob, CodeProfile, DBConversionProfile, Match, Server, Wave, Workload,
    DB_DIFFICULTY_C, DB_DIFFICULTY_B,
    MJS_TESTED, MJS_READY_FOR_CUTOVER, MJS_CUT_OVER, MJS_FINALIZED,
    MJS_ROLLED_BACK, MJS_PLANNED, MJS_REPLICATING,
    STAGE_LZ,
)

GREEN, YELLOW, RED, NA = "green", "yellow", "red", "n/a"
_LEVELS = (GREEN, YELLOW, RED)


def _worst(levels: List[str]) -> str:
    """Worst-case rollup: red > yellow > green (n/a ignored)."""
    if RED in levels:
        return RED
    if YELLOW in levels:
        return YELLOW
    if GREEN in levels:
        return GREEN
    return NA


# ---------------------------------------------------------------------------
# individual signals (each returns {level, detail})
# ---------------------------------------------------------------------------
def _sig_lz_ready(store, wave: Wave, servers: List[Server],
                  matches: List[Match]) -> Dict[str, Any]:
    """F8 — the wave's target archetypes are all finalized."""
    from .lz import check_lz_gate
    if wave.stage == STAGE_LZ:
        return {"level": GREEN, "detail": "LZ wave — not gated on LZ readiness"}
    gate = check_lz_gate(store, wave.id, servers, matches)
    if gate["ok"]:
        return {"level": GREEN, "detail": "all target archetypes finalized"}
    blocking = gate.get("blocking_archetypes", [])
    return {"level": RED, "detail": f"LZ not ready: {', '.join(blocking)}"}


def _sig_db_conversion(wave: Wave, servers: List[Server],
                       dbidx: Dict[str, DBConversionProfile]) -> Dict[str, Any]:
    """F5 — db hosts assessed + no grade-C blocker outstanding."""
    db_members = [s for s in servers if s.id in (wave.server_ids or [])
                  and (s.role or "").lower() == "db"]
    if not db_members:
        return {"level": GREEN, "detail": "no DB hosts in wave"}
    grades: List[str] = []
    unassessed = 0
    for s in db_members:
        dp = None
        for k in (s.hostname.lower().strip(), s.identity_key, s.id):
            dp = dbidx.get(k)
            if dp:
                break
        if not dp:
            unassessed += 1
            continue
        grades.append(dp.difficulty)
    if DB_DIFFICULTY_C in grades:
        return {"level": RED,
                "detail": f"{grades.count(DB_DIFFICULTY_C)} DB host(s) grade C (hard conversion outstanding)"}
    if unassessed > 0 or DB_DIFFICULTY_B in grades:
        bits = []
        if unassessed:
            bits.append(f"{unassessed} unassessed")
        if DB_DIFFICULTY_B in grades:
            bits.append(f"{grades.count(DB_DIFFICULTY_B)} grade B (manual review)")
        return {"level": YELLOW, "detail": " + ".join(bits)}
    return {"level": GREEN, "detail": "all DB hosts assessed (grade A)"}


def _sig_code_refactor(wave: Wave, servers: List[Server],
                       pidx: Dict[str, CodeProfile],
                       changejobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """executor — apps profiled, no blockers, a done ChangeJob each."""
    app_ids = set()
    for s in servers:
        if s.id in (wave.server_ids or []):
            app_ids.update(s.app_ids or [])
    if not app_ids:
        return {"level": GREEN, "detail": "no apps bound to wave members"}
    done_jobs = {(j.get("app_id"), j.get("kind")) for j in changejobs
                 if j.get("status") == "done"}
    missing_profile = 0
    blockers = 0
    no_modify_done = 0
    for a in app_ids:
        p = pidx.get(a)
        if not p:
            missing_profile += 1
            continue
        if p.blockers:
            blockers += len(p.blockers)
        if (a, "modify") not in done_jobs and (a, "comb") not in done_jobs:
            no_modify_done += 1
    if blockers > 0:
        return {"level": RED, "detail": f"{blockers} code blocker(s) outstanding"}
    if missing_profile > 0:
        return {"level": YELLOW, "detail": f"{missing_profile} app(s) not profiled"}
    if no_modify_done > 0:
        return {"level": YELLOW,
                "detail": f"{no_modify_done} app(s) no done modify/comb ChangeJob"}
    return {"level": GREEN, "detail": "all apps profiled, no blockers, ChangeJob done"}


def _sig_deps_resolved(store, wave: Wave, waves: List[Wave],
                       servers: List[Server], workloads: List[Workload]
                       ) -> Dict[str, Any]:
    """F1 + DAG — depends_on waves are done; no orphan code dep.

    Green when every wave the current wave depends on is itself ``done``
    (finalized rollup) and every code_dep of the wave's apps is to an app in a
    predecessor wave. Yellow when a dep wave is still running. Red when a dep
    wave hasn't launched / rolled back, or an app has a code_dep to an app not
    in the estate (orphan — something left behind)."""
    from .execute import wave_execution_status, _dominant_status
    if not wave.depends_on:
        dep_status = "green"
    else:
        dep_states = []
        for dep_wid in wave.depends_on:
            roll = wave_execution_status(store, dep_wid)
            dep_states.append(roll.get("dominant_status", "planned"))
        if any(s in ("planned", "rolled-back") for s in dep_states):
            return {"level": RED,
                    "detail": f"depends_on wave(s) not done: {dep_states}"}
        if any(s == "running" for s in dep_states):
            return {"level": YELLOW, "detail": f"depends_on wave(s) running: {dep_states}"}
        return {"level": GREEN, "detail": "all depends_on waves done"}
    # check orphan code_deps: an app's code_dep to an app not in any wave
    app_ids_in_waves = {a for w in waves for s in servers if s.id in (w.server_ids or [])
                        for a in (s.app_ids or [])}
    member_apps = {a for s in servers if s.id in (wave.server_ids or [])
                   for a in (s.app_ids or [])}
    pidx_apps = {w.app_id for w in workloads}
    orphans = []
    for wl in workloads:
        if wl.app_id not in member_apps:
            continue
        for dep in (wl.depends_on or []):
            if dep not in app_ids_in_waves and dep not in pidx_apps:
                orphans.append(dep)
    if orphans:
        return {"level": RED, "detail": f"{len(orphans)} orphan code dep(s): {orphans[:5]}"}
    return {"level": GREEN, "detail": "no depends_on, no orphan deps"}


def _sig_cutover_rehearsal(wave: Wave, mjobs_by_server: Dict[str, Any]
                           ) -> Dict[str, Any]:
    """F6 — every member MigrationJob reached ``tested`` or beyond."""
    members = wave.server_ids or []
    if not members:
        return {"level": NA, "detail": "empty wave"}
    statuses = [mjobs_by_server.get(sid).status if mjobs_by_server.get(sid)
               else "not_launched" for sid in members]
    if all(s == "not_launched" for s in statuses):
        return {"level": RED, "detail": "wave not launched (no MigrationJobs)"}
    rehearsed = [s for s in statuses if s in (MJS_TESTED, MJS_READY_FOR_CUTOVER,
                                             MJS_CUT_OVER, MJS_FINALIZED)]
    if len(rehearsed) == len(members):
        return {"level": GREEN, "detail": "all servers reached tested+"}
    if len(rehearsed) > 0:
        return {"level": YELLOW,
                "detail": f"{len(rehearsed)}/{len(members)} servers reached tested+"}
    return {"level": RED, "detail": f"0/{len(members)} servers rehearsed"}


def _sig_rollback_channel(wave: Wave, servers: List[Server],
                           dbidx: Dict[str, DBConversionProfile],
                           mjobs_by_server: Dict[str, Any]) -> Dict[str, Any]:
    """F5 — db jobs have reverse_replication on (cutover rollback net)."""
    db_members = [s for s in servers if s.id in (wave.server_ids or [])
                  and (s.role or "").lower() == "db"]
    if not db_members:
        return {"level": GREEN, "detail": "no DB hosts — no rollback channel needed"}
    no_channel = 0
    for s in db_members:
        dp = None
        for k in (s.hostname.lower().strip(), s.identity_key, s.id):
            dp = dbidx.get(k)
            if dp:
                break
        if not dp or not dp.reverse_replication:
            no_channel += 1
    if no_channel == len(db_members):
        return {"level": RED, "detail": "no DB host has reverse replication on"}
    if no_channel > 0:
        return {"level": YELLOW,
                "detail": f"{no_channel}/{len(db_members)} DB host(s) lack reverse replication"}
    return {"level": GREEN, "detail": "all DB hosts have reverse replication"}


def _sig_hw_support(wave: Wave, servers: List[Server]) -> Dict[str, Any]:
    """F10 + data-gap — member hardware is under warranty through the cutover
    window. Traditional-IDC reality: CMDB often has no support data.

    Worst-case across members: ``expired`` → red (out-of-warranty hardware is a
    cutover + refresh risk), ``expiring`` (<90d to end-of-support) → yellow
    (cutover deadline), ``active`` → green, ``unknown`` → n/a (not assessed;
    doesn't gate cutover — the blind spot is surfaced in the data-gaps report
    instead, not here). n/a is ignored by ``_worst`` so existing waves with no
    warranty data keep their rollup unchanged.
    """
    from .match import warranty_bucket
    members = [s for s in servers if s.id in (wave.server_ids or [])]
    if not members:
        return {"level": NA, "detail": "empty wave"}
    _bucket_to_level = {WARRANTY_EXPIRED: RED, WARRANTY_EXPIRING: YELLOW,
                        WARRANTY_ACTIVE: GREEN}
    buckets = [warranty_bucket(s) for s in members]
    levels = [_bucket_to_level.get(b, NA) for b in buckets]
    rank = {RED: 0, YELLOW: 1, GREEN: 2}
    present = [lv for lv in levels if lv in rank]
    if not present:
        return {"level": NA, "detail": "hardware support not assessed (no EOL data)"}
    worst = min(present, key=lambda lv: rank[lv])
    n_exp = buckets.count(WARRANTY_EXPIRED)
    n_exp2 = buckets.count(WARRANTY_EXPIRING)
    n_act = buckets.count(WARRANTY_ACTIVE)
    if worst == RED:
        return {"level": RED,
                "detail": f"{n_exp} host(s) out of warranty — cutover + refresh risk"}
    if worst == YELLOW:
        return {"level": YELLOW,
                "detail": f"{n_exp2} host(s) warranty expiring <{WARRANTY_EXPIRING_DAYS}d"}
    return {"level": GREEN,
            "detail": f"{n_act} host(s) under warranty"}


def _sig_os_support(wave: Wave, servers: List[Server]) -> Dict[str, Any]:
    """F10 + data-gap — member OS is under vendor support through cutover.

    Traditional-IDC reality: CentOS 6/7, Windows Server 2008/2012, old Oracle
    Linux are out of support and a silent rehost is a migration failure (no
    supported cloud image). Same worst-case as ``_sig_hw_support``: expired→red
    (silent-rehost risk), expiring→yellow (replatform deadline), active→green,
    unknown→n/a (OS not in the EOL table — not flagged). n/a ignored by rollup.
    """
    from .eol import os_eol_bucket, EXPIRED as OS_EXPIRED, EXPIRING as OS_EXPIRING, ACTIVE as OS_ACTIVE
    members = [s for s in servers if s.id in (wave.server_ids or [])]
    if not members:
        return {"level": NA, "detail": "empty wave"}
    _bucket_to_level = {OS_EXPIRED: RED, OS_EXPIRING: YELLOW, OS_ACTIVE: GREEN}
    buckets = [os_eol_bucket(s) for s in members]
    levels = [_bucket_to_level.get(b, NA) for b in buckets]
    rank = {RED: 0, YELLOW: 1, GREEN: 2}
    present = [lv for lv in levels if lv in rank]
    if not present:
        return {"level": NA, "detail": "OS support not assessed (OS not in EOL table)"}
    worst = min(present, key=lambda lv: rank[lv])
    n_exp = buckets.count(OS_EXPIRED)
    n_exp2 = buckets.count(OS_EXPIRING)
    if worst == RED:
        return {"level": RED,
                "detail": f"{n_exp} host(s) on an out-of-support OS — replatform, do not rehost"}
    if worst == YELLOW:
        return {"level": YELLOW,
                "detail": f"{n_exp2} host(s) OS nearing end-of-support"}
    return {"level": GREEN, "detail": "all host OS under vendor support"}


# ---------------------------------------------------------------------------
# per-wave readiness
# ---------------------------------------------------------------------------
def wave_readiness(store, wave_id: str) -> Dict[str, Any]:
    """Per-wave readiness heatmap (F10). Returns ``{wave_id, wave_name, stage,
    signals: {name: {level, detail}}, rollup, can_cutover}``.

    ``rollup`` is the worst signal level; ``can_cutover`` is True only when
    rollup is green (red blocks cutover, yellow = proceed-with-caution).
    """
    wave = None
    waves = store.list_waves()
    for w in waves:
        if w.id == wave_id:
            wave = w
            break
    if wave is None:
        return {"wave_id": wave_id, "error": "wave not found"}
    servers = store.list_all_servers()
    matches = store.list_matches()
    workloads = store.list_workloads()
    profiles = store.list_code_profiles()
    db_profiles = store.list_db_profiles()
    changejobs = store.list_change_jobs(limit=1000)
    mjobs = store.list_migration_jobs(wave_id=wave_id)
    pidx = index_profiles(profiles)
    dbidx = {d.db_server_id: d for d in db_profiles}
    mjobs_by_server = {j.server_id: j for j in mjobs}

    signals = {
        "lz_ready": _sig_lz_ready(store, wave, servers, matches),
        "db_conversion": _sig_db_conversion(wave, servers, dbidx),
        "code_refactor": _sig_code_refactor(wave, servers, pidx, changejobs),
        "deps_resolved": _sig_deps_resolved(store, wave, waves, servers, workloads),
        "cutover_rehearsal": _sig_cutover_rehearsal(wave, mjobs_by_server),
        "rollback_channel": _sig_rollback_channel(wave, servers, dbidx, mjobs_by_server),
        "hw_support": _sig_hw_support(wave, servers),
        "os_support": _sig_os_support(wave, servers),
    }
    levels = [s["level"] for s in signals.values()]
    rollup = _worst(levels)
    return {
        "wave_id": wave_id, "wave_name": wave.name, "stage": wave.stage,
        "server_count": len(wave.server_ids or []),
        "signals": signals, "rollup": rollup,
        "can_cutover": rollup == GREEN,
    }


def portfolio_readiness(store) -> List[Dict[str, Any]]:
    """Readiness heatmap for every wave (F10). LZ + disposition waves
    (retain/retire) are included but their cutover-readiness is informational."""
    out = []
    for w in store.list_waves():
        out.append(wave_readiness(store, w.id))
    return out


__all__ = ["wave_readiness", "portfolio_readiness",
           "GREEN", "YELLOW", "RED", "NA"]