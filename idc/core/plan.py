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
from typing import Dict, List, Optional, Set

from .models import (
    Server,
    STAGE_APP,
    STAGE_CUTOVER,
    STAGE_DB,
    STAGE_LZ,
    Wave,
    Workload,
)


def _topo_order(workloads: List[Workload]) -> List[Workload]:
    by_id = {w.app_id: w for w in workloads}
    indeg = {w.app_id: 0 for w in workloads}
    children: Dict[str, List[str]] = defaultdict(list)
    for w in workloads:
        for dep in w.depends_on:
            if dep in by_id:
                indeg[w.app_id] += 1
                children[dep].append(w.app_id)
    q = deque(sorted([a for a, d in indeg.items() if d == 0]))
    order: List[str] = []
    while q:
        a = q.popleft()
        order.append(a)
        for c in sorted(children[a]):
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    # any cycles → append remainder deterministically
    for a in by_id:
        if a not in order:
            order.append(a)
    return [by_id[a] for a in order]


def _has_dependents(app_id: str, workloads: List[Workload]) -> bool:
    return any(app_id in w.depends_on for w in workloads)


def plan_waves(servers: List[Server], workloads: Optional[List[Workload]] = None) -> List[Wave]:
    workloads = workloads or []
    by_id = {s.id: s for s in servers}
    waves: List[Wave] = []

    # --- stage 1: landing zone / platform -------------------------------
    lz_servers = [s.id for s in servers if (s.role or "") in ("k8s", "monitoring")]
    lz_wave = Wave(name="W1 Landing Zone + Platform", stage=STAGE_LZ,
                   server_ids=lz_servers,
                   rationale="Stand up TKE, VPC/COS bootstrap, and observability first.")
    waves.append(lz_wave)

    # --- stage 2: data services -----------------------------------------
    data_servers = [s.id for s in servers if (s.role or "") in ("db", "cache")]
    data_wave = Wave(name="W2 Data Services", stage=STAGE_DB,
                     server_ids=data_servers, depends_on=[lz_wave.id],
                     rationale="Migrate DBs (CDB) and cache (Redis) before apps depend on them.")
    waves.append(data_wave)

    # --- stage 3/4: applications by DAG ---------------------------------
    # A server belongs to exactly one primary wave. Role-based stages
    # (k8s/monitoring → LZ, db/cache → Data) win over app grouping, so infra
    # hosts are NOT duplicated into their app's wave.
    infra_ids = set(lz_servers) | set(data_servers)

    # map app -> server ids (from resolved workloads; exclude infra hosts)
    app_to_servers: Dict[str, List[str]] = defaultdict(list)
    for w in workloads:
        app_to_servers[w.app_id] = [sid for sid in w.server_ids
                                    if sid in by_id and sid not in infra_ids]
    for s in servers:
        for a in s.app_ids:
            if s.id not in infra_ids and s.id not in app_to_servers[a]:
                app_to_servers[a].append(s.id)

    app_to_wave: Dict[str, str] = {}
    for w in _topo_order(workloads):
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
        wv = Wave(name=name, stage=stage, server_ids=sids,
                  depends_on=list(dict.fromkeys(deps)),
                  rationale=(w.notes or f"Migrate {w.app_id} after its dependencies "
                                        f"({', '.join(w.depends_on) or 'none'})."))
        app_to_wave[w.app_id] = wv.id
        waves.append(wv)

    # anything still unassigned (no role stage, no app) → catch-all after data
    assigned = {sid for wv in waves for sid in wv.server_ids}
    leftover = [s.id for s in servers if s.id not in assigned]
    if leftover:
        waves.append(Wave(
            name=f"W{len(waves)+1} General", stage=STAGE_APP,
            server_ids=leftover, depends_on=[data_wave.id],
            rationale="Servers without an app graph entry; migrate after data services."))

    return waves


def wave_dag(waves: List[Wave]) -> Dict[str, List[str]]:
    """Return adjacency: wave_id -> [dependent wave_ids]."""
    children: Dict[str, List[str]] = defaultdict(list)
    for w in waves:
        for d in w.depends_on:
            children[d].append(w.id)
    return {k: v for k, v in children.items()}