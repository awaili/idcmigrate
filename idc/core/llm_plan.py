"""LLM-driven wave planning — policy engine.

The LLM acts as an *architect*: it turns a natural-language demand into a
structured ``WavePolicy`` (an ordered list of ``WaveSpec``-s). This module is
the deterministic *executor* that instantiates a policy over the full server
estate (15K+) into concrete ``Wave``-s, plus a validator.

Keeping the LLM on policy-generation (not wave-enumeration) is what makes this
scale: the LLM emits ~4-10 specs, the engine handles 15K servers verifiably.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .match import REGION_MAP  # noqa: F401  (re-export convenience)
from .models import (
    Match,
    Server,
    STAGE_APP,
    STAGE_CUTOVER,
    STAGE_DB,
    STAGE_LZ,
    Wave,
    Workload,
    _new_id,
)


@dataclass
class WaveSpec:
    """One stage in the policy. The engine assigns every matching,
    not-yet-assigned server to a wave derived from this spec."""
    label: str                              # unique, human-readable
    stage: str = STAGE_APP                  # one of STAGE_* or custom
    rationale: str = ""
    # selection filters (all lists are OR-within, AND-across)
    roles: List[str] = field(default_factory=list)
    envs: List[str] = field(default_factory=list)
    os: List[str] = field(default_factory=list)
    target_products: List[str] = field(default_factory=list)
    criticalities: List[str] = field(default_factory=list)
    clusters: List[str] = field(default_factory=list)
    regions: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)        # server has ANY of these tags
    app_ids: List[str] = field(default_factory=list)
    # grouping / sizing
    group_by: str = "none"    # none|app|cluster|env|region|role|target
    max_per_wave: int = 0     # 0 = no cap
    # dependency: labels of specs that must complete before this spec's waves
    depends_on: List[str] = field(default_factory=list)
    # use the app dependency DAG within this spec (only meaningful with group_by=app)
    use_app_deps: bool = True

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WaveSpec":
        return cls(
            label=str(d.get("label") or d.get("name") or ""),
            stage=str(d.get("stage") or STAGE_APP),
            rationale=str(d.get("rationale") or d.get("why") or ""),
            roles=_as_list(d.get("roles") or d.get("role")),
            envs=_as_list(d.get("envs") or d.get("env")),
            os=_as_list(d.get("os")),
            target_products=_as_list(d.get("target_products") or d.get("targets") or d.get("target")),
            criticalities=_as_list(d.get("criticalities") or d.get("criticality")),
            clusters=_as_list(d.get("clusters") or d.get("cluster")),
            regions=_as_list(d.get("regions") or d.get("region")),
            tags=_as_list(d.get("tags") or d.get("tag")),
            app_ids=_as_list(d.get("app_ids") or d.get("apps")),
            group_by=str(d.get("group_by") or d.get("groupBy") or "none").lower(),
            max_per_wave=int(d.get("max_per_wave") or d.get("maxPerWave") or 0),
            depends_on=_as_list(d.get("depends_on") or d.get("dependsOn")),
            use_app_deps=bool(d.get("use_app_deps", True)),
        )

    def matches(self, s: Server, m: Optional[Match],
                srv_to_app: Optional[Dict[str, str]] = None) -> bool:
        if self.roles and s.role not in self.roles:
            return False
        if self.envs and s.env not in self.envs:
            return False
        if self.os and s.os not in self.os:
            return False
        if self.criticalities and s.business_criticality not in self.criticalities:
            return False
        if self.clusters and s.cluster not in self.clusters:
            return False
        if self.target_products and (not m or m.target.product not in self.target_products):
            return False
        if self.regions and (not m or m.target.region not in self.regions):
            return False
        if self.tags and not any(t in s.tags for t in self.tags):
            return False
        if self.app_ids:
            apps = set(s.app_ids or [])
            if srv_to_app:
                a = srv_to_app.get(s.id)
                if a:
                    apps.add(a)
            if not any(a in self.app_ids for a in apps):
                return False
        return True


@dataclass
class WavePolicy:
    stages: List[WaveSpec] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WavePolicy":
        stages = d.get("stages") or d.get("specs") or d.get("waves") or []
        if isinstance(stages, dict):
            stages = [stages]
        return cls(stages=[WaveSpec.from_dict(s) for s in stages],
                   notes=str(d.get("notes") or ""))

    def validate_schema(self) -> List[str]:
        errs: List[str] = []
        labels = [s.label for s in self.stages]
        if not labels:
            errs.append("policy has no stages")
        if len(set(labels)) != len(labels):
            errs.append(f"duplicate stage labels: {labels}")
        known = set(labels)
        for s in self.stages:
            for dep in s.depends_on:
                if dep not in known:
                    errs.append(f"stage '{s.label}' depends_on unknown '{dep}'")
        return errs


@dataclass
class PlanReport:
    waves: List[Wave]
    assigned: Set[str]
    unassigned: List[str]
    total: int
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self, servers_by_id: Dict[str, Server]) -> Dict[str, Any]:
        return {
            "waves": [w.to_dict() for w in self.waves],
            "stats": {"total": self.total, "assigned": len(self.assigned),
                      "unassigned": len(self.unassigned), "waves": len(self.waves)},
            "errors": self.errors, "warnings": self.warnings,
            "unassigned_hosts": [servers_by_id.get(sid, type("", (), {"hostname": sid})()).hostname
                                 for sid in self.unassigned[:50]],
        }


def _as_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


def _group_key(spec: WaveSpec, s: Server, m: Optional[Match],
               srv_to_app: Optional[Dict[str, str]] = None) -> str:
    gb = spec.group_by
    if gb == "none":
        return "all"
    if gb == "app":
        if s.app_ids:
            return s.app_ids[0]
        if srv_to_app and srv_to_app.get(s.id):
            return srv_to_app[s.id]
        return "_no_app"
    if gb == "cluster":
        return s.cluster or "_no_cluster"
    if gb == "env":
        return s.env or "_no_env"
    if gb == "region":
        return (m.target.region if m else "_no_region") or "_no_region"
    if gb == "role":
        return s.role or "_no_role"
    if gb == "target":
        return (m.target.product if m else "_no_target") or "_no_target"
    return "all"


def build_from_policy(policy: WavePolicy, servers: List[Server],
                      workloads: Optional[List[Workload]] = None,
                      matches: Optional[List[Match]] = None,
                      scope_ids: Optional[Set[str]] = None) -> PlanReport:
    """Instantiate a policy into concrete waves over the estate."""
    workloads = workloads or []
    m_by_id = {m.server_id: m for m in (matches or [])}
    by_id = {s.id: s for s in servers}
    scope = scope_ids if scope_ids is not None else set(s.id for s in servers)
    # server -> its app_id, derived from workloads (sources don't carry app_ids)
    srv_to_app: Dict[str, str] = {}
    for w in workloads:
        for sid in w.server_ids:
            srv_to_app.setdefault(sid, w.app_id)

    errors = list(policy.validate_schema())
    waves: List[Wave] = []
    assigned: Set[str] = set()
    # label -> list of wave ids produced for that spec (for depends_on resolution)
    spec_waves: Dict[str, List[str]] = defaultdict(list)

    # app dependency map (app_id -> depends_on app_ids)
    app_deps: Dict[str, List[str]] = {w.app_id: list(w.depends_on) for w in workloads}
    # app_id -> wave id (filled as app-grouped waves are created, for intra-spec deps)
    app_wave: Dict[str, str] = {}

    # pass 1: assign servers to waves in array order (first-match partitioning).
    # Cross-spec depends_on is resolved in pass 2 so a stage may depend on a
    # later-arrayed stage (common when specific stages are ordered before a
    # generic catch-all for partitioning, but must run after it).
    wave_specs: List[Tuple[Wave, WaveSpec]] = []
    for spec in policy.stages:
        if not spec.label:
            errors.append("a stage has no label; skipped")
            continue
        # selection
        selected = [s for s in servers
                    if s.id in scope and s.id not in assigned
                    and spec.matches(s, m_by_id.get(s.id), srv_to_app)]
        if not selected:
            spec_waves[spec.label] = []
            continue
        # grouping
        groups: Dict[str, List[Server]] = defaultdict(list)
        for s in selected:
            groups[_group_key(spec, s, m_by_id.get(s.id), srv_to_app)].append(s)

        # order groups: when group_by=app, topological by app deps; else by name
        if spec.group_by == "app" and spec.use_app_deps and app_deps:
            group_names = _topo_apps(list(groups.keys()), app_deps)
        else:
            group_names = sorted(groups.keys())

        for gname in group_names:
            members = groups[gname]
            chunk_sz = spec.max_per_wave if spec.max_per_wave > 0 else len(members)
            chunks = [members[i:i + chunk_sz] for i in range(0, len(members), chunk_sz)]
            for ci, chunk in enumerate(chunks):
                sids = [s.id for s in chunk]
                # intra-spec app dependency (resolved now, same-spec)
                intra: List[str] = []
                if spec.group_by == "app" and spec.use_app_deps and gname != "_no_app":
                    for dep_app in app_deps.get(gname, []):
                        if dep_app in app_wave:
                            intra.append(app_wave[dep_app])
                name = spec.label if (len(groups) == 1 and len(chunks) == 1) else \
                    f"{spec.label} · {gname}" + (f" · p{ci+1}" if len(chunks) > 1 else "")
                w = Wave(name=name, stage=spec.stage, server_ids=sids,
                         depends_on=list(dict.fromkeys(intra)),
                         rationale=spec.rationale or spec.label)
                waves.append(w)
                wave_specs.append((w, spec))
                spec_waves[spec.label].append(w.id)
                for sid in sids:
                    assigned.add(sid)
                if spec.group_by == "app" and gname != "_no_app":
                    app_wave[gname] = w.id

    # pass 2: resolve cross-spec depends_on (prepend to any intra-spec deps)
    for w, spec in wave_specs:
        cross: List[str] = []
        for dep in spec.depends_on:
            cross += spec_waves.get(dep, [])
        w.depends_on = list(dict.fromkeys(cross + w.depends_on))

    unassigned = [sid for sid in scope if sid not in assigned]
    warnings: List[str] = []
    if unassigned:
        warnings.append(f"{len(unassigned)} servers matched no stage; added to a catch-all wave.")
        waves.append(Wave(name="Unassigned", stage=STAGE_APP,
                          server_ids=unassigned, depends_on=[],
                          rationale="Servers not matched by any policy stage."))

    report = PlanReport(waves=waves, assigned=assigned, unassigned=[],
                        total=len(scope), errors=errors, warnings=warnings)
    if unassigned:
        report.unassigned = unassigned
        report.assigned = assigned  # all assigned via catch-all
    return report


def _topo_apps(apps: List[str], app_deps: Dict[str, List[str]]) -> List[str]:
    """Topological order of app ids, respecting app_deps (deps first)."""
    from collections import deque
    appset = set(apps)
    indeg = {a: 0 for a in apps}
    children: Dict[str, List[str]] = defaultdict(list)
    for a in apps:
        for dep in app_deps.get(a, []):
            if dep in appset:
                indeg[a] += 1
                children[dep].append(a)
    q = deque(sorted([a for a, d in indeg.items() if d == 0]))
    out: List[str] = []
    while q:
        a = q.popleft()
        out.append(a)
        for c in sorted(children[a]):
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    for a in apps:
        if a not in out:
            out.append(a)
    return out


def validate_plan(waves: List[Wave], servers: List[Server]) -> Dict[str, Any]:
    """Static validation of a proposed/loaded plan."""
    by_id = {s.id: s for s in servers}
    errors: List[str] = []
    warnings: List[str] = []
    seen: Dict[str, str] = {}
    for w in waves:
        for sid in w.server_ids:
            if sid not in by_id:
                errors.append(f"wave '{w.name}' references unknown server {sid}")
                continue
            if sid in seen:
                errors.append(f"server {by_id[sid].hostname} ({sid}) in two waves: "
                              f"'{seen[sid]}' and '{w.name}'")
            seen[sid] = w.name
    # depends_on existence + acyclic
    wid = {w.id: w for w in waves}
    for w in waves:
        for d in w.depends_on:
            if d not in wid:
                errors.append(f"wave '{w.name}' depends on unknown wave {d}")
    if not _is_dag(waves):
        errors.append("wave dependency graph has a cycle")
    assigned = len(seen)
    total = len(servers)
    if assigned < total:
        warnings.append(f"{total - assigned} of {total} servers not in any wave")
    if assigned > total:
        errors.append(f"{assigned - total} extra assignments (duplicates counted)")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "assigned": assigned, "total": total, "waves": len(waves)}


def _is_dag(waves: List[Wave]) -> bool:
    children: Dict[str, List[str]] = defaultdict(list)
    indeg: Dict[str, int] = {w.id: 0 for w in waves}
    for w in waves:
        for d in w.depends_on:
            if d in indeg:
                children[d].append(w.id)
                indeg[w.id] += 1
    from collections import deque
    q = deque([wid for wid, d in indeg.items() if d == 0])
    seen = 0
    while q:
        n = q.popleft(); seen += 1
        for c in children[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    return seen == len(waves)


def apply_plan(store, waves: List[Wave]) -> None:
    """Persist a plan: wipe existing waves and write the new ones, atomically.

    Delegates to ``Store.replace_waves`` so the DELETE + inserts run in one
    transaction (can't leave an empty waves table plus a partial plan) and the
    SQL is dialect-correct for both sqlite and mariadb.
    """
    store.replace_waves(waves)