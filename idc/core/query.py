"""Deterministic estate query engine for the grounded copilot.

The copilot's value is that graph/aggregate answers are computed by THESE pure
functions (reproducible, no hallucination), and the LLM only (a) classifies a
natural-language question into one of the intents and (b) narrates the
structured result. The LLM never does graph traversal or counting itself.

Intents (the LLM picks one + extracts params):
  deps_of           {app_id}        what does the app depend on (apps + waves)
  dependents_of      {app_id}        what depends on the app
  whatif_delayed     {app_id}        if the app is delayed, which apps/waves block
  wave_for_app       {app_id}        which wave(s) contain the app's servers
  riskiest_waves     {n?}            top-N waves by deterministic risk score
  aggregate          {scope, metric} distribution (env/criticality/role/target)
  retain_retire                      apps marked retain/retire + why
  search             {q}             keyword fallback over servers
  unknown                            couldn't classify — returns a hint

All functions are pure (no DB, no network) so they unit-test directly.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set

from .codeintel import wave_risk_basis
from .models import CodeProfile, Match, Server, Wave, Workload


# the canonical intent list — exported so the LLM classifier prompt + the
# dispatcher stay in sync
INTENTS = (
    "deps_of", "dependents_of", "whatif_delayed", "wave_for_app",
    "riskiest_waves", "aggregate", "retain_retire", "search", "unknown",
)


def _app_to_servers(workloads: List[Workload], servers: List[Server]) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = defaultdict(set)
    for w in workloads:
        out[w.app_id].update(w.server_ids or [])
    for s in servers:
        for a in (s.app_ids or []):
            out[a].add(s.id)
    return out


def _server_to_waves(waves: List[Wave]) -> Dict[str, List[str]]:
    """server_id -> list of wave ids containing it."""
    out: Dict[str, List[str]] = defaultdict(list)
    for w in waves:
        for sid in (w.server_ids or []):
            out[sid].append(w.id)
    return out


def _app_dep_graph(workloads: List[Workload]) -> Dict[str, List[str]]:
    return {w.app_id: list(w.depends_on or []) for w in workloads}


def _reverse_reachable(app_id: str, deps: Dict[str, List[str]]) -> Set[str]:
    """Apps that (transitively) depend on app_id — i.e. blocked if app_id is
    delayed. BFS over the reverse of the depends_on edges."""
    rev: Dict[str, List[str]] = defaultdict(list)
    for app, ds in deps.items():
        for d in ds:
            rev[d].append(app)
    seen: Set[str] = set()
    q = deque([app_id])
    while q:
        a = q.popleft()
        for child in rev.get(a, []):
            if child not in seen and child != app_id:
                seen.add(child)
                q.append(child)
    return seen


def estate_query(intent: str, params: Optional[Dict[str, Any]],
                 *, servers: List[Server], waves: List[Wave],
                 workloads: List[Workload], matches: List[Match],
                 profiles: List[CodeProfile], strategies: Dict[str, Any]
                 ) -> Dict[str, Any]:
    """Run one deterministic estate query. ``strategies`` is {app_id: AppStrategy}
    (or {} ). Returns a structured dict the LLM narrates. Unknown intent ->
    {intent:"unknown", hint:...} so the LLM can fall back to free text."""
    params = params or {}
    intent = (intent or "unknown").strip().lower()
    by_id = {s.id: s for s in servers}
    app_srv = _app_to_servers(workloads, servers)
    srv_wave = _server_to_waves(waves)
    deps = _app_dep_graph(workloads)
    wave_by_id = {w.id: w for w in waves}

    def _app_waves(app_id: str) -> List[str]:
        sids = app_srv.get(app_id, set())
        wids: List[str] = []
        for sid in sids:
            for wid in srv_wave.get(sid, []):
                if wid not in wids:
                    wids.append(wid)
        return wids

    if intent == "deps_of":
        a = params.get("app_id", "")
        return {"intent": intent, "app_id": a,
                "depends_on": deps.get(a, []),
                "depends_on_waves": _app_waves_many(deps.get(a, []), _app_waves)}

    if intent == "dependents_of":
        a = params.get("app_id", "")
        rev = [app for app, ds in deps.items() if a in ds]
        return {"intent": intent, "app_id": a, "dependents": rev}

    if intent == "whatif_delayed":
        a = params.get("app_id", "")
        blocked = sorted(_reverse_reachable(a, deps))
        blocked_waves = sorted({w for app in blocked for w in _app_waves(app)})
        return {"intent": intent, "app_id": a,
                "blocked_apps": blocked, "blocked_app_count": len(blocked),
                "blocked_waves": [wave_by_id[w].name for w in blocked_waves
                                  if w in wave_by_id]}

    if intent == "wave_for_app":
        a = params.get("app_id", "")
        wids = _app_waves(a)
        return {"intent": intent, "app_id": a,
                "server_count": len(app_srv.get(a, set())),
                "waves": [{"id": wid, "name": wave_by_id[wid].name,
                           "stage": wave_by_id[wid].stage}
                          for wid in wids if wid in wave_by_id]}

    if intent == "riskiest_waves":
        n = int(params.get("n", 5) or 5)
        ranked = sorted(
            ((wave_risk_basis(w, servers, matches, profiles), w) for w in waves),
            key=lambda t: t[0]["score"], reverse=True)
        return {"intent": intent, "top_n": n,
                "waves": [{"name": w.name, "stage": w.stage,
                           "score": b["score"], "level": b["level"],
                           "factors": b["factors"], "n_servers": len(w.server_ids)}
                          for b, w in ranked[:n]]}

    if intent == "aggregate":
        scope = (params.get("scope") or "all").lower()   # all|cutover|stage:<x>
        metric = (params.get("metric") or "env").lower()  # env|criticality|role|target
        if scope == "cutover":
            pool = [s for w in waves if w.stage == "4_cutover" for s in w.server_ids]
            pool = [by_id[sid] for sid in pool if sid in by_id]
        elif scope.startswith("stage:"):
            st = scope.split(":", 1)[1]
            pool = [by_id[sid] for w in waves if w.stage == st
                    for sid in (w.server_ids or []) if sid in by_id]
        else:
            pool = servers
        m_by_id = {m.server_id: m for m in matches}
        dist: Dict[str, int] = defaultdict(int)
        for s in pool:
            if metric == "env":
                k = s.env or "(none)"
            elif metric == "criticality":
                k = s.business_criticality or "(none)"
            elif metric == "role":
                k = s.role or "(none)"
            elif metric == "target":
                m = m_by_id.get(s.id)
                k = (m.target.product if m and m.target else "(unmatched)")
            else:
                k = "(unknown)"
            dist[k] += 1
        return {"intent": intent, "scope": scope, "metric": metric,
                "total": len(pool),
                "distribution": dict(sorted(dist.items(), key=lambda x: -x[1]))}

    if intent == "retain_retire":
        items = []
        for aid, s in (strategies or {}).items():
            if getattr(s, "strategy", "") in ("retain", "retire"):
                items.append({"app_id": aid, "strategy": s.strategy,
                              "target": getattr(s, "target", ""),
                              "rationale": getattr(s, "rationale", "")})
        # legacy fallback: profiles with retain/retire
        if not items:
            for p in profiles:
                if p.migration_pattern in ("retain", "retire"):
                    items.append({"app_id": p.app_id,
                                  "strategy": p.migration_pattern,
                                  "target": "", "rationale": p.summary or ""})
        return {"intent": intent, "count": len(items), "apps": items}

    if intent == "search":
        q = (params.get("q") or "").lower().strip()
        if not q:
            return {"intent": intent, "q": "", "matches": []}
        hits = []
        total = 0
        # Token-aware matching so a natural-language q like "oracle database"
        # still hits. The estate tags DB/middleware hosts as ``oracle-db``,
        # ``db:postgres``, ``mw:tomcat`` … — the bare phrase "oracle database"
        # never appears verbatim, so a pure substring search returns zero.
        # We (1) canonicalize multi-word tech phrases to their tag form, (2) drop
        # filler words, (3) fold synonyms (database→db, middleware→mw) and (4)
        # split each tag on -/: so "oracle-db" exposes "oracle" + "db" as tokens.
        # A server matches when the whole (canonicalized) phrase is a substring
        # OR every surviving token is present.
        #
        # The phrase map is load-bearing for precision: "oracle" is ambiguous on
        # this estate — it is both an Oracle DB (tag ``oracle-db``) AND an OS
        # (``oracle linux``). Without it, "oracle database" token-matches "oracle"
        # (from the OS) + "db" (role) and returns MySQL/Mongo hosts that merely
        # run on Oracle Linux. Mapping "oracle database"→"oracle-db" makes the
        # match hit the tag exactly, excluding those false positives.
        phrase_map = {
            "oracle database": "oracle-db", "oracle db": "oracle-db",
            "sql server": "sqlserver", "sqlserver database": "sqlserver",
        }
        for phrase, canon in phrase_map.items():
            q = q.replace(phrase, canon)
        stop = {"the", "a", "an", "of", "and", "or", "for", "with", "is", "are",
                "hosted", "hosting", "hosts", "host", "server", "servers",
                "running", "run", "which", "what", "all", "list", "show", "find"}
        synonyms = {"database": "db", "databases": "db", "dbs": "db",
                    "middleware": "mw", "appserver": "tomcat",
                    "app-server": "tomcat", "cache": "cache"}
        qtokens = []
        for w in q.split():
            w = w.strip(".,?;:()\"'")
            if not w or w in stop:
                continue
            if w in synonyms:
                w = synonyms[w]
            if w and w not in qtokens:
                qtokens.append(w)
        for s in servers:
            tags = list(s.tags or [])
            tech_words = []
            for t in tags:
                for seg in re.split(r"[-:/]+", t):
                    if seg:
                        tech_words.append(seg)
            hay = " ".join([s.hostname or "", s.fqdn or "", s.role or "", s.os or "",
                            s.env or "", " ".join(tags), " ".join(s.app_ids or []),
                            " ".join(tech_words)]).lower()
            if q in hay or (qtokens and all(tok in hay for tok in qtokens)):
                total += 1
                if len(hits) < 30:   # cap the payload, but keep the true total
                    hits.append({"hostname": s.hostname, "role": s.role, "env": s.env,
                                 "app_ids": s.app_ids, "os": s.os, "tags": tags})
        return {"intent": intent, "q": params.get("q", ""), "matches": hits,
                "count": total, "shown": len(hits),
                "truncated": total > len(hits)}

    return {"intent": "unknown",
            "hint": "unrecognized intent; answer from general context if you can, "
                    "otherwise say you couldn't map the question to an estate query.",
            "valid_intents": list(INTENTS)}


def _app_waves_many(app_ids: List[str], fn) -> List[str]:
    out: List[str] = []
    for a in app_ids:
        for w in fn(a):
            if w not in out:
                out.append(w)
    return out