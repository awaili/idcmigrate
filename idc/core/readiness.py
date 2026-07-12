"""F10 — portfolio readiness heatmap (per-wave "is it safe to cut?" scorecard).

Collapses the scattered migration signals into one green/yellow/red heatmap per
wave, à la AWS CAF MRA / Azure CAF "is this wave ready to cutover?". The score is
deterministic — every signal reads from persisted state (no LLM) — so the
"safe to cut?" answer is reproducible/auditable and the LLM only narrates it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from .codeintel import index_profiles
from .execute import _dominant_status, MIGRATION_JOB_STATUSES
from .lz import wave_archetypes, LZ_FINALIZED
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
_NOT_LAUNCHED = "not_launched"


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
# signal helpers (used by both single-wave and portfolio paths)
# ---------------------------------------------------------------------------
def _db_profile_for(s: Server, dbidx: Dict[str, DBConversionProfile]) -> Optional[DBConversionProfile]:
    """Look up a DB conversion profile by hostname, identity_key, or server id."""
    for k in (s.hostname.lower().strip(), s.identity_key, s.id):
        if k and dbidx.get(k):
            return dbidx[k]
    return None


def _sig_lz_ready(wave: Wave, servers: List[Server], matches: List[Match],
                  lz_statuses: Dict[str, str]) -> Dict[str, Any]:
    """F8 — the wave's target archetypes are all finalized."""
    if wave.stage == STAGE_LZ:
        return {"level": GREEN, "detail": "LZ wave — not gated on LZ readiness"}
    mix = wave_archetypes(wave.server_ids or [], servers, matches)
    blocking = [a for a in mix if mix.get(a, 0) > 0
                and lz_statuses.get(a) != LZ_FINALIZED]
    if not blocking:
        return {"level": GREEN, "detail": "all target archetypes finalized"}
    return {"level": RED, "detail": f"LZ not ready: {', '.join(blocking)}"}


def _sig_db_conversion(wave_members: List[Server],
                       dbidx: Dict[str, DBConversionProfile]) -> Dict[str, Any]:
    """F5 — db hosts assessed + no grade-C blocker outstanding."""
    db_members = [s for s in wave_members if (s.role or "").lower() == "db"]
    if not db_members:
        return {"level": GREEN, "detail": "no DB hosts in wave"}
    grades: List[str] = []
    unassessed = 0
    for s in db_members:
        dp = _db_profile_for(s, dbidx)
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


def _sig_code_refactor(member_app_ids: Set[str],
                       pidx: Dict[str, CodeProfile],
                       done_jobs: Set[Tuple[str, str]]) -> Dict[str, Any]:
    """executor — apps profiled, no blockers, a done ChangeJob each."""
    if not member_app_ids:
        return {"level": GREEN, "detail": "no apps bound to wave members"}
    missing_profile = 0
    blockers = 0
    no_modify_done = 0
    for a in member_app_ids:
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


def _sig_deps_resolved(wave: Wave,
                       dominant_by_wave: Dict[str, str],
                       member_app_ids: Set[str],
                       orphan_app_ids: Set[str]) -> Dict[str, Any]:
    """F1 + DAG — depends_on waves are done; no orphan code dep."""
    if wave.depends_on:
        dep_states = [dominant_by_wave.get(wid, "planned") for wid in wave.depends_on]
        if any(s in ("planned", "rolled-back") for s in dep_states):
            return {"level": RED,
                    "detail": f"depends_on wave(s) not done: {dep_states}"}
        if any(s == "running" for s in dep_states):
            return {"level": YELLOW, "detail": f"depends_on wave(s) running: {dep_states}"}
    if member_app_ids & orphan_app_ids:
        orphans = sorted(member_app_ids & orphan_app_ids)
        return {"level": RED,
                "detail": f"{len(orphans)} orphan code dep(s): {orphans[:5]}"}
    return {"level": GREEN, "detail": "no depends_on, no orphan deps"}


def _sig_cutover_rehearsal(members: List[str],
                           mjobs_by_server: Dict[str, Any]) -> Dict[str, Any]:
    """F6 — every member MigrationJob reached ``tested`` or beyond."""
    if not members:
        return {"level": NA, "detail": "empty wave"}
    statuses = [mjobs_by_server.get(sid).status if mjobs_by_server.get(sid)
                else _NOT_LAUNCHED for sid in members]
    if all(s == _NOT_LAUNCHED for s in statuses):
        return {"level": RED, "detail": "wave not launched (no MigrationJobs)"}
    rehearsed = [s for s in statuses if s in (MJS_TESTED, MJS_READY_FOR_CUTOVER,
                                              MJS_CUT_OVER, MJS_FINALIZED)]
    if len(rehearsed) == len(members):
        return {"level": GREEN, "detail": "all servers reached tested+"}
    if rehearsed:
        return {"level": YELLOW,
                "detail": f"{len(rehearsed)}/{len(members)} servers reached tested+"}
    return {"level": RED, "detail": f"0/{len(members)} servers rehearsed"}


def _sig_rollback_channel(wave_members: List[Server],
                          dbidx: Dict[str, DBConversionProfile]) -> Dict[str, Any]:
    """F5 — db jobs have reverse_replication on (cutover rollback net)."""
    db_members = [s for s in wave_members if (s.role or "").lower() == "db"]
    if not db_members:
        return {"level": GREEN, "detail": "no DB hosts — no rollback channel needed"}
    no_channel = 0
    for s in db_members:
        dp = _db_profile_for(s, dbidx)
        if not dp or not dp.reverse_replication:
            no_channel += 1
    if no_channel == len(db_members):
        return {"level": RED, "detail": "no DB host has reverse replication on"}
    if no_channel > 0:
        return {"level": YELLOW,
                "detail": f"{no_channel}/{len(db_members)} DB host(s) lack reverse replication"}
    return {"level": GREEN, "detail": "all DB hosts have reverse replication"}


def _sig_hw_support(wave_or_members, servers: Optional[List[Server]] = None
                    ) -> Dict[str, Any]:
    """F10 + data-gap — member hardware is under warranty through the cutover.

    Backward-compatible: accepts either a ``Wave`` + ``servers`` list (legacy
    tests) or a pre-computed list of member servers (portfolio fast path).
    """
    from .match import warranty_bucket
    if servers is not None:
        sids = set(wave_or_members.server_ids or [])
        members = [s for s in servers if s.id in sids]
    else:
        members = wave_or_members
    if not members:
        return {"level": NA, "detail": "empty wave"}
    # UNKNOWN -> YELLOW (conservative): an unassessed estate must NOT read as
    # green/ready on the cutover gate — that hides exactly the blind spot most
    # dangerous on a messy IDC. (The confidence score treats unknown as neutral;
    # the readiness GATE is a different consumer and must be conservative.)
    _bucket_to_level = {WARRANTY_EXPIRED: RED, WARRANTY_EXPIRING: YELLOW,
                        WARRANTY_ACTIVE: GREEN, WARRANTY_UNKNOWN: YELLOW}
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
    n_unk = buckets.count(WARRANTY_UNKNOWN)
    if worst == RED:
        return {"level": RED,
                "detail": f"{n_exp} host(s) out of warranty — cutover + refresh risk"}
    if n_unk and not n_exp2:
        return {"level": YELLOW,
                "detail": f"{n_unk} host(s) warranty status unassessed — verify before cutover"}
    if worst == YELLOW:
        return {"level": YELLOW,
                "detail": f"{n_exp2} host(s) warranty expiring <{WARRANTY_EXPIRING_DAYS}d"}
    return {"level": GREEN, "detail": f"{n_act} host(s) under warranty"}


def _sig_os_support(wave_or_members, servers: Optional[List[Server]] = None
                    ) -> Dict[str, Any]:
    """F10 + data-gap — member OS is under vendor support through cutover.

    Backward-compatible: accepts either a ``Wave`` + ``servers`` list (legacy
    tests) or a pre-computed list of member servers (portfolio fast path).
    """
    from .eol import (os_eol_bucket, EXPIRED as OS_EXPIRED, EXPIRING as OS_EXPIRING,
                      ACTIVE as OS_ACTIVE, UNKNOWN as OS_UNKNOWN)
    if servers is not None:
        sids = set(wave_or_members.server_ids or [])
        members = [s for s in servers if s.id in sids]
    else:
        members = wave_or_members
    if not members:
        return {"level": NA, "detail": "empty wave"}
    # UNKNOWN -> YELLOW (conservative): an OS not in the EOL table is an
    # unassessed blind spot, not "safe to cut". See _sig_hw_support for rationale.
    _bucket_to_level = {OS_EXPIRED: RED, OS_EXPIRING: YELLOW, OS_ACTIVE: GREEN,
                        OS_UNKNOWN: YELLOW}
    buckets = [os_eol_bucket(s) for s in members]
    levels = [_bucket_to_level.get(b, NA) for b in buckets]
    rank = {RED: 0, YELLOW: 1, GREEN: 2}
    present = [lv for lv in levels if lv in rank]
    if not present:
        return {"level": NA, "detail": "OS support not assessed (OS not in EOL table)"}
    worst = min(present, key=lambda lv: rank[lv])
    n_exp = buckets.count(OS_EXPIRED)
    n_exp2 = buckets.count(OS_EXPIRING)
    n_unk = buckets.count(OS_UNKNOWN)
    if worst == RED:
        return {"level": RED,
                "detail": f"{n_exp} host(s) on an out-of-support OS — replatform, do not rehost"}
    if n_unk and not n_exp2:
        return {"level": YELLOW,
                "detail": f"{n_unk} host(s) OS not assessed against the EOL table — verify before cutover"}
    if worst == YELLOW:
        return {"level": YELLOW,
                "detail": f"{n_exp2} host(s) OS nearing end-of-support"}
    return {"level": GREEN, "detail": "all host OS under vendor support"}


# ---------------------------------------------------------------------------
# pre-computed state for portfolio_readiness (one full-DB pass)
# ---------------------------------------------------------------------------
def _build_readiness_state(store) -> Dict[str, Any]:
    """Load the persisted estate once and build the indexes the readiness
    signals need. Makes ``portfolio_readiness`` O(waves + servers + jobs)
    instead of O(waves * (servers + jobs)) per wave."""
    waves = store.list_waves()
    servers = store.list_all_servers()
    matches = store.list_matches()
    workloads = store.list_workloads()
    profiles = store.list_code_profiles()
    db_profiles = store.list_db_profiles()
    changejobs = store.list_change_jobs(status="done")
    mjobs_all = store.list_migration_jobs()
    lz_statuses = store.list_lz_status()

    srv_by_id = {s.id: s for s in servers}

    # wave membership + apps per wave
    wave_members: Dict[str, List[Server]] = {}
    member_app_ids: Dict[str, Set[str]] = {}
    all_apps_in_waves: Set[str] = set()
    for w in waves:
        members = [srv_by_id[sid] for sid in (w.server_ids or []) if sid in srv_by_id]
        wave_members[w.id] = members
        apps = {a for s in members for a in (s.app_ids or [])}
        member_app_ids[w.id] = apps
        all_apps_in_waves.update(apps)

    workload_app_ids = {w.app_id for w in workloads}
    orphan_app_ids: Set[str] = set()
    for wl in workloads:
        for dep in (wl.depends_on or []):
            if dep not in all_apps_in_waves and dep not in workload_app_ids:
                orphan_app_ids.add(wl.app_id)

    # code-profile index + done change jobs
    pidx = index_profiles(profiles)
    done_jobs = {(j.get("app_id"), j.get("kind")) for j in changejobs
                 if j.get("status") == "done"}

    # DB profile index — keyed by db_server_id (a Server.id, or a hostname when
    # the executor keyed it that way). _db_profile_for looks up by hostname /
    # identity_key / id, all of which are Server-side tokens that must match
    # db_server_id, so a single key suffices.
    dbidx: Dict[str, DBConversionProfile] = {d.db_server_id: d for d in db_profiles}

    # migration jobs grouped by wave + dominant status per wave
    mjobs_by_wave: Dict[str, List[Any]] = {}
    for j in mjobs_all:
        mjobs_by_wave.setdefault(j.wave_id, []).append(j)
    dominant_by_wave: Dict[str, str] = {}
    for wid, jobs in mjobs_by_wave.items():
        counts: Dict[str, int] = {s: 0 for s in MIGRATION_JOB_STATUSES}
        counts[_NOT_LAUNCHED] = 0
        for j in jobs:
            counts[j.status] = counts.get(j.status, 0) + 1
        dominant_by_wave[wid] = _dominant_status(counts)

    return {
        "waves": waves,
        "servers": servers,
        "matches": matches,
        "workloads": workloads,
        "pidx": pidx,
        "dbidx": dbidx,
        "done_jobs": done_jobs,
        "wave_members": wave_members,
        "member_app_ids": member_app_ids,
        "all_apps_in_waves": all_apps_in_waves,
        "workload_app_ids": workload_app_ids,
        "orphan_app_ids": orphan_app_ids,
        "mjobs_by_wave": mjobs_by_wave,
        "dominant_by_wave": dominant_by_wave,
        "lz_statuses": lz_statuses,
    }


def _wave_readiness_fast(wave: Wave, state: Dict[str, Any]) -> Dict[str, Any]:
    """Compute one wave's readiness from the pre-loaded ``_build_readiness_state``
    indexes. Mirrors ``wave_readiness`` exactly."""
    members = state["wave_members"].get(wave.id, [])
    member_apps = state["member_app_ids"].get(wave.id, set())
    mjobs_by_server = {j.server_id: j for j in state["mjobs_by_wave"].get(wave.id, [])}

    signals = {
        "lz_ready": _sig_lz_ready(wave, state["servers"], state["matches"],
                                  state["lz_statuses"]),
        "db_conversion": _sig_db_conversion(members, state["dbidx"]),
        "code_refactor": _sig_code_refactor(member_apps, state["pidx"],
                                            state["done_jobs"]),
        "deps_resolved": _sig_deps_resolved(
            wave, state["dominant_by_wave"], member_apps, state["orphan_app_ids"]),
        "cutover_rehearsal": _sig_cutover_rehearsal(
            wave.server_ids or [], mjobs_by_server),
        "rollback_channel": _sig_rollback_channel(members, state["dbidx"]),
        "hw_support": _sig_hw_support(members),
        "os_support": _sig_os_support(members),
    }
    rollup = _worst([s["level"] for s in signals.values()])
    return {
        "wave_id": wave.id, "wave_name": wave.name, "stage": wave.stage,
        "server_count": len(wave.server_ids or []),
        "signals": signals, "rollup": rollup,
        "can_cutover": rollup == GREEN,
    }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def wave_readiness(store, wave_id: str) -> Dict[str, Any]:
    """Per-wave readiness heatmap (F10). Returns ``{wave_id, wave_name, stage,
    signals: {name: {level, detail}}, rollup, can_cutover}``.

    This path is used for one wave at a time (``/api/readiness/{wave_id}``), so
    it loads just that wave's state. The portfolio path below pre-loads
    everything once for scalability.
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
    changejobs = store.list_change_jobs(status="done")
    mjobs = store.list_migration_jobs(wave_id=wave_id)

    srv_by_id = {s.id: s for s in servers}
    members = [srv_by_id[sid] for sid in (wave.server_ids or []) if sid in srv_by_id]
    member_apps = {a for s in members for a in (s.app_ids or [])}
    pidx = index_profiles(profiles)
    dbidx: Dict[str, DBConversionProfile] = {d.db_server_id: d for d in db_profiles}
    done_jobs = {(j.get("app_id"), j.get("kind")) for j in changejobs
                 if j.get("status") == "done"}
    mjobs_by_server = {j.server_id: j for j in mjobs}

    # dominant status of depends_on waves (loaded on demand)
    dominant_by_wave: Dict[str, str] = {}
    if wave.depends_on:
        for dep_wid in wave.depends_on:
            dep_jobs = store.list_migration_jobs(wave_id=dep_wid)
            counts = {s: 0 for s in MIGRATION_JOB_STATUSES}
            counts[_NOT_LAUNCHED] = 0
            for j in dep_jobs:
                counts[j.status] = counts.get(j.status, 0) + 1
            dominant_by_wave[dep_wid] = _dominant_status(counts)

    all_apps_in_waves = {a for w in waves for s in srv_by_id.values()
                         if s.id in (w.server_ids or [])
                         for a in (s.app_ids or [])}
    workload_app_ids = {w.app_id for w in workloads}
    orphan_app_ids: Set[str] = set()
    for wl in workloads:
        for dep in (wl.depends_on or []):
            if dep not in all_apps_in_waves and dep not in workload_app_ids:
                orphan_app_ids.add(wl.app_id)

    signals = {
        "lz_ready": _sig_lz_ready(wave, servers, matches, store.list_lz_status()),
        "db_conversion": _sig_db_conversion(members, dbidx),
        "code_refactor": _sig_code_refactor(member_apps, pidx, done_jobs),
        "deps_resolved": _sig_deps_resolved(
            wave, dominant_by_wave, member_apps, orphan_app_ids),
        "cutover_rehearsal": _sig_cutover_rehearsal(wave.server_ids or [], mjobs_by_server),
        "rollback_channel": _sig_rollback_channel(members, dbidx),
        "hw_support": _sig_hw_support(members),
        "os_support": _sig_os_support(members),
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
    (retain/retire) are included but their cutover-readiness is informational.

    Loads the persisted estate exactly once and reuses indexes across waves,
    so large portfolios don't time out.
    """
    state = _build_readiness_state(store)
    return [_wave_readiness_fast(w, state) for w in state["waves"]]


__all__ = ["wave_readiness", "portfolio_readiness",
           "GREEN", "YELLOW", "RED", "NA"]
