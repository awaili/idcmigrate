"""Wave planning: group servers into migration waves with a dependency DAG.

Strategy (standard cloud-migration wave model):
  1. Landing Zone  — platform/infra servers (TKE, monitoring) that must stand
     up before anything else.
  2. Data          — DBs + cache, which apps depend on.
  3. Application   — app/web servers grouped by application, ordered by the
     app dependency DAG (topological sort). Each app is one wave and depends
     on its dependency apps' waves + the data wave.
  4. Cutover       — top-of-DAG frontends that depend on everything else;
     these are the last traffic-switch.

Waves reference server ids and other wave ids (``depends_on``) so the UI can
render the DAG and the agent can execute in order.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Callable, Dict, List, Optional, Set

from .codeintel import (
    ProfileIndex,
    disposition_waves,
    effort_rank,
    index_profiles,
    merge_code_deps,
    non_migrating_servers,
    pattern_rank,
    wave_rationale_extra,
)
from .models import (
    AppStrategy,
    CodeProfile,
    Server,
    STAGE_APP,
    STAGE_CUTOVER,
    STAGE_DB,
    STAGE_LZ,
    Wave,
    Workload,
)


def _topo_order(workloads: List[Workload],
                order_key: Optional[Callable[[str], object]] = None) -> List[Workload]:
    """Kahn's algorithm. ``order_key`` breaks ties among ready nodes so
    cheaper-to-migrate apps release before expensive ones within a level."""
    by_id = {w.app_id: w for w in workloads}
    indeg = {w.app_id: 0 for w in workloads}
    children: Dict[str, List[str]] = defaultdict(list)
    for w in workloads:
        for dep in w.depends_on:
            if dep in by_id:
                indeg[w.app_id] += 1
                children[dep].append(w.app_id)

    def _sort(apps: List[str]) -> List[str]:
        if order_key:
            return sorted(apps, key=lambda a: (order_key(a), a))
        return sorted(apps)

    q = deque(_sort([a for a, d in indeg.items() if d == 0]))
    order: List[str] = []
    while q:
        a = q.popleft()
        order.append(a)
        ready = [c for c in children[a] if indeg[c] - 1 == 0]
        for c in children[a]:
            indeg[c] -= 1
        for c in _sort(ready):
            q.append(c)
    # any cycles → append remainder deterministically
    for a in by_id:
        if a not in order:
            order.append(a)
    return [by_id[a] for a in order]


def _has_dependents(app_id: str, workloads: List[Workload]) -> bool:
    return any(app_id in w.depends_on for w in workloads)


def plan_waves(servers: List[Server], workloads: Optional[List[Workload]] = None,
               code_profiles: Optional[List[CodeProfile]] = None,
               max_waves: int = 0,
               strategies: Optional[Dict[str, "AppStrategy"]] = None,
               min_assessment_confidence: float = 0.0) -> List[Wave]:
    """Group servers into migration waves with a dependency DAG.

    ``code_profiles`` (from the external agent executor) enrich the plan:
      * code-discovered deps are merged into ``workloads.depends_on`` so the
        DAG reflects real call edges the static graph missed;
      * within a topological level, apps are ordered by refactor effort /
        migration pattern (cheap rehosts first, refactors/rewrites later);
      * each app wave's rationale carries a one-line code-intel annotation
        (pattern / effort / readiness / blocker count).

    ``max_waves`` — when >0, a HARD cap on the total wave count: if the
    deterministic plan exceeds it, :func:`cap_waves` merges waves down (never
    creating a cycle). This unifies the cap guarantee with the LLM path
    (``build_from_policy``) so "the operator said N waves -> at most N" holds
    on Rebuild too, not just on plan-llm. Note: cap_waves merges structurally
    (incomparable/level collapse); for semantically nicer batching use the
    LLM planner (``plan-llm``).

    ``strategies`` — optional ``{app_id: AppStrategy}`` overlay (AI-assigned
    7R). retain/retire apps are pulled from this overlay first, then from
    ``code_profiles.migration_pattern`` (legacy). Lets a 7R result drive wave
    exclusion WITHOUT first persisting to the DB.

    ``min_assessment_confidence`` — data-gap soft gate (default 0.0 = off).
    When >0, servers whose ``assessment_confidence`` is below the threshold
    are pulled out of the migration sequence into a trailing "Needs Discovery"
    wave (not in the DAG) so thinly-known hosts (traditional-IDC reality: no
    role / no util / no profile) don't get a cutover slot before they've been
    characterized. None-confidence hosts (not yet rebuilt) are NOT gated — only
    a measured-low confidence triggers the gate. The cap (``max_waves``) is
    applied after the discovery wave is appended.
    """
    workloads = workloads or []
    pidx: ProfileIndex = index_profiles(code_profiles or [])
    if pidx:
        # merge code deps (idempotent — deduped) so the topo sort sees them
        merge_code_deps(workloads, code_profiles or [])
        order_key = lambda a: (  # noqa: E731  cheap-first within a level
            effort_rank(pidx.get(a)), pattern_rank(pidx.get(a)))
    else:
        order_key = None
    by_id = {s.id: s for s in servers}
    waves: List[Wave] = []

    # --- 7R non-migrating disposition: pull retain/retire servers OUT of the
    # migration sequence. They never enter LZ/Data/App/Cutover waves; they go
    # to trailing disposition waves at the end. A server is non-migrating only
    # if EVERY app it belongs to is the same disposition (shared servers with a
    # migrating app still migrate). Servers with no app binding are never here.
    retain_ids, retire_ids = non_migrating_servers(servers, workloads,
                                                   code_profiles or [], strategies)
    excluded = retain_ids | retire_ids

    # --- data-gap soft confidence gate: pull thinly-known hosts OUT of the
    # migration sequence into a trailing "Needs Discovery" wave. Only fires
    # when min_assessment_confidence > 0; a None confidence (not rebuilt) is
    # left alone so the gate never drops a host that simply hasn't been scored.
    discovery_ids: Set[str] = set()
    if min_assessment_confidence > 0:
        discovery_ids = {s.id for s in servers
                         if s.assessment_confidence is not None
                         and s.assessment_confidence < min_assessment_confidence}
        excluded = excluded | discovery_ids

    # --- stage 1: landing zone / platform -------------------------------
    lz_servers = [s.id for s in servers
                  if (s.role or "").lower() in ("k8s", "monitoring") and s.id not in excluded]
    lz_wave = Wave(name="W1 Landing Zone + Platform", stage=STAGE_LZ,
                   server_ids=lz_servers,
                   rationale="Stand up TKE, VPC/COS bootstrap, and observability first.")
    waves.append(lz_wave)

    # --- stage 2: data services -----------------------------------------
    data_servers = [s.id for s in servers
                    if (s.role or "").lower() in ("db", "cache") and s.id not in excluded]
    data_wave = Wave(name="W2 Data Services", stage=STAGE_DB,
                     server_ids=data_servers, depends_on=[lz_wave.id],
                     rationale="Migrate DBs (CDB) and cache (Redis) before apps depend on them.")
    waves.append(data_wave)

    # --- stage 3/4: applications by DAG ---------------------------------
    # A server belongs to exactly one primary wave. Role-based stages
    # (k8s/monitoring → LZ, db/cache → Data) win over app grouping, so infra
    # hosts are NOT duplicated into their app's wave.
    infra_ids = set(lz_servers) | set(data_servers)

    # map app -> server ids (from resolved workloads; exclude infra + non-migrating)
    app_to_servers: Dict[str, List[str]] = defaultdict(list)
    for w in workloads:
        app_to_servers[w.app_id] = [sid for sid in w.server_ids
                                    if sid in by_id and sid not in infra_ids
                                    and sid not in excluded]
    for s in servers:
        for a in s.app_ids:
            if s.id not in infra_ids and s.id not in excluded \
                    and s.id not in app_to_servers[a]:
                app_to_servers[a].append(s.id)

    app_to_wave: Dict[str, str] = {}
    for w in _topo_order(workloads, order_key=order_key):
        sids = app_to_servers.get(w.app_id, [])
        if not sids:
            # app's servers are all infra (moved in LZ/Data) → no own wave
            app_to_wave[w.app_id] = data_wave.id if any(
                sid in data_servers for sid in (w.server_ids or [])) else lz_wave.id
            continue
        is_frontend = ((w.tier or "").lower() in ("frontend", "web")
                       and not _has_dependents(w.app_id, workloads))
        deps = [data_wave.id] + [app_to_wave[d] for d in w.depends_on if d in app_to_wave]
        stage = STAGE_CUTOVER if is_frontend else STAGE_APP
        label = "Cutover" if stage == STAGE_CUTOVER else "App"
        name = f"W{len(waves)+1} {label}: {w.name or w.app_id}"
        base_rationale = (w.notes or f"Migrate {w.app_id} after its dependencies "
                                       f"({', '.join(w.depends_on) or 'none'}).")
        extra = wave_rationale_extra(w.app_id, pidx)
        wv = Wave(name=name, stage=stage, server_ids=sids,
                  depends_on=list(dict.fromkeys(deps)),
                  rationale=(base_rationale + (f" [{extra}]" if extra else "")))
        app_to_wave[w.app_id] = wv.id
        waves.append(wv)

    # anything still unassigned (no role stage, no app, not retain/retire)
    # → catch-all after data
    assigned = {sid for wv in waves for sid in wv.server_ids}
    leftover = [s.id for s in servers if s.id not in assigned and s.id not in excluded]
    if leftover:
        waves.append(Wave(
            name=f"W{len(waves)+1} General", stage=STAGE_APP,
            server_ids=leftover, depends_on=[data_wave.id],
            rationale="Servers without an app graph entry; migrate after data services."))

    # --- trailing non-migrating disposition waves (7R retain/retire) ---
    # No depends_on: these are not part of the migration sequence. Surfaced
    # explicitly so every server is still accounted for in the plan. The wave
    # construction is shared (codeintel.disposition_waves); plan_waves prefixes
    # the W-number to match its numbering scheme.
    for w in disposition_waves(retain_ids, retire_ids):
        w.name = f"W{len(waves)+1} {w.name}"
        waves.append(w)

    # --- data-gap: trailing "Needs Discovery" wave for thinly-known hosts.
    # Like the disposition waves it's NOT in the migration DAG (no depends_on):
    # these hosts don't get a cutover slot until the operator improves their
    # data quality (run discovery / ingest telemetry / profile the app) and
    # re-plans. Surfaced explicitly so the gap is visible, not silently dropped.
    if discovery_ids:
        waves.append(Wave(
            name=f"W{len(waves)+1} Needs Discovery", stage=STAGE_APP,
            server_ids=sorted(discovery_ids),
            rationale=f"{len(discovery_ids)} host(s) below min_assessment_confidence "
                      f"— characterize (role/util/profile) before cutover."))

    # hard cap on total wave count (unifies the guarantee with the LLM path).
    # Applied AFTER retain/retire disposition waves are appended, so the cap
    # counts every wave. Function-level import avoids a plan <-> llm_plan cycle.
    if max_waves > 0 and len(waves) > max_waves:
        from .llm_plan import cap_waves
        waves, _ = cap_waves(waves, max_waves)

    return waves


def wave_dag(waves: List[Wave]) -> Dict[str, List[str]]:
    """Return adjacency: wave_id -> [dependent wave_ids]."""
    children: Dict[str, List[str]] = defaultdict(list)
    for w in waves:
        for d in w.depends_on:
            children[d].append(w.id)
    return {k: v for k, v in children.items()}