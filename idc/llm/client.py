"""LLM gateway client + migration-specific prompts.

Talks to the local Ollama-compatible gateway (fronting cloud models such as
``glm-5.2:cloud``). All calls degrade gracefully: if the gateway is down or
``IDC_LLM_ENABLED=false``, callers get a clear "LLM unavailable" marker and
the rule-based pipeline keeps working without LLM enrichment.

Capabilities:
  * ``chat``            — raw chat completion (streaming optional)
  * ``explain_match``   — natural-language rationale for a Match
  * ``ask``             — RAG-style Q&A over the inventory
  * ``summarize_wave``  — exec brief for a wave
  * ``right_size``      — second opinion on a CVM/CDB spec given utilization
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

import httpx

from ..config import Settings, get_settings
from ..core.models import ALL_PATTERNS_7R, CodeProfile, Match, Server, Wave, Workload

UNAVAILABLE = "[MigraQ unavailable — rule-based result only]"


SEVEN_R_SYSTEM = """You are a cloud-migration strategist for Tencent Cloud. After resources are
reconsolidated, you assign ONE migration strategy (the 7R model) to an
application, given its consolidated context (servers, workload/deps, matched
Tencent targets, code profile).

The 7R strategies (output ``strategy`` MUST be exactly one of these):
  rehost            lift-and-shift to CVM as-is, NO code change. Use for cloud-
                    ready, portable apps where speed/low-risk matters.
  rehost-container  lift-and-shift BUT containerize and run on TKE. Minimal
                    change (Dockerfile + externalized config), no re-architecture.
                    Prefer for stateless/elastic apps that fit a container.
  replatform        minor code changes to use managed services (self-hosted
                    MySQL->CDB, Consul->service discovery, local cron->TKE cron).
  refactor          meaningful re-architecture (microservices, serverless, SCF).
                    High effort; use when the business needs cloud-native scale.
  repurchase        replace with a SaaS/managed product (self-hosted Kafka->CKafka,
                    Jenkins->CODING, Gitlab->CODING Repos). Use when a managed
                    equivalent removes operational burden.
  retain            keep on-prem — regulated data, mainframe, hard latency tie to
                    on-prem, or not worth migrating. NOT a migration target.
  retire            decommission — unused, redundant, sunset. NOT a migration
                    target.

Decision guidance:
- Stateless + cloud-ready + low effort -> rehost or rehost-container.
- Stateless + elastic + container-friendly runtime -> rehost-container (TKE).
- Stateful DB/cache -> replatform to managed (CDB / TencentDB for Redis).
- Hard blockers / legacy runtime / low cloud readiness -> refactor, repurchase,
  retain, or retire depending on business value.
- Never invent a strategy outside the 7R list.

Output ONLY a JSON object, no prose, no markdown fences:
  {"strategy": "<one of the 7R>", "rationale": "<1-3 sentences why>",
   "target": "<Tencent product e.g. CVM/TKE/CDB, or 'on-prem'/'decommission'>",
   "confidence": <0..1>, "effort": "<low|medium|high>",
   "key_changes": ["<short change 1>", ...]}"""


AUDIT_SYSTEM = """You are a cloud-migration architect auditing a rule-based target mapping
for Tencent Cloud. Given a source server, the rule engine's chosen target (product
+ spec + confidence + rationale), and the app's code profile (if any), critique
whether the rule got it right.

Tencent Cloud targets and when they fit:
  CVM        generic VM (lift-and-shift). Default for stateless app/web/general roles.
  TKE        managed Kubernetes. For stateless, container-friendly, elastic apps that
             fit a pod — NOT for stateful DBs or hard-stateful workloads.
  CDB        managed MySQL/PostgreSQL/SQL Server. For db-role servers running a
             supported engine. Self-hosted DB on a CVM is usually wrong if CDB fits.
  TencentDB for Redis / Memcached  for cache-role. Self-hosted Redis on CVM is usually
             wrong if the managed product fits.
  CFS        shared file storage (NFS). For stateful_local / shared-disk workloads.
  COS        object storage. For static assets / backups / large immutable blobs.
  EMR        managed big-data (Hadoop/Spark/Hive). For hadoop-role clusters.
  BM         bare metal. For workloads needing direct hardware access / licensing
             (specialized HPC, Oracle RAC, appliances) that don't fit a VM.
  CLB / CDN  load balancing / edge — usually accessories, not a server's primary target.

Audit logic:
- If the rule target matches the server's role + workload shape + code profile,
  verdict = "keep" (don't second-guess a correct call).
- If a clearly better target exists (e.g. db-role mapped to CVM should be CDB;
  stateless container-friendly app mapped to CVM could be TKE; hadoop mapped to
  CVM should be EMR), verdict = "change" and name alternative_target (+spec).
- If signals conflict or are too thin to judge (mixed role, no profile, weird
  appliance), verdict = "review" — flag it for a human, don't guess.
- Hard blockers (legacy runtime, baremetal-only licensing) should push toward
  BM or "review", not a naive CVM.
- Never invent a product outside the list above.

Output ONLY a JSON object, no prose, no markdown fences:
  {"verdict": "keep" | "change" | "review",
   "confidence": <0..1>,                  # your confidence in the verdict
   "critique": "<1-3 sentences: what's right/wrong with the rule target>",
   "alternative_target": "<Tencent product or null>",
   "alternative_spec": "<spec hint or null>",
   "rationale": "<why, esp. if change>",
   "risks": ["<migration risk 1>", ...]}"""


def _audit_context(server: Server, match: Match,
                    profile: Optional[CodeProfile]) -> str:
    """Compact context for the match auditor — server attrs + rule target +
    code profile, as JSON. Mirrors _seven_r_context's shape."""
    tgt = match.target
    payload: Dict[str, Any] = {
        "server": _server_brief(server),
        "rule_match": {
            "product": tgt.product, "spec": tgt.spec, "region": tgt.region,
            "confidence": match.confidence, "rationale": match.rationale,
            "alternatives": list(match.alternatives or []),
        },
    }
    if profile is not None:
        payload["code_profile"] = {
            "language": profile.language, "runtime": profile.runtime,
            "framework": profile.framework,
            "cloud_readiness": profile.cloud_readiness,
            "executor_pattern": profile.migration_pattern,
            "blockers": list(profile.blockers or []),
            "summary": profile.summary,
        }
    return ("Audit the rule-based migration target for this server, given its "
            f"context (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "Output ONLY the audit JSON object.")


ASSESS_WAVE_SYSTEM = """You are a migration wave lead producing a go/no-go + runbook for ONE migration
wave. You are given the wave (name/stage/depends_on) + a DETERMINISTIC risk
basis (score/level/factors/signals — computed from the real members; treat it as
authoritative, do NOT recompute it) + a sample of the wave's members (servers
with role/target/utilization/criticality).

Produce:
  go_no_go  : "go" (ready to migrate), "hold" (one or two fixable blockers), or
              "no-go" (blocking risk — e.g. unresolved code blockers, high
              criticality + no rollback, dependency not done). Base it on the
              risk level + factors, not vibes.
  pre_checks : list of concrete pre-cutover checks for THIS wave (e.g. "confirm
              CDB HA sync lag < 1s for the {n} db servers", "drain <app> canary
              traffic before flipping {product}"). Grounded in the members.
  cutover    : ordered cutover steps for this wave.
  rollback   : rollback steps if the wave fails.
  summary    : 2-4 sentence exec brief naming the riskiest aspect.

Keep each list item to one short imperative sentence. No prose outside JSON.

Output ONLY a JSON object, no markdown fences:
  {"go_no_go": "go"|"hold"|"no-go",
   "pre_checks": ["...", ...], "cutover": ["...", ...],
   "rollback": ["...", ...], "summary": "..."}"""


def _assess_wave_context(wave: Wave, servers: List[Server], matches: List[Match],
                         basis: Dict[str, Any]) -> str:
    """Compact context for the wave assessor — wave + deterministic risk basis +
    a sample of members, as JSON."""
    by_id = {s.id: s for s in servers}
    m_by_id = {m.server_id: m for m in (matches or [])}
    members = [by_id[sid] for sid in wave.server_ids if sid in by_id]
    sample = []
    for s in members[:60]:
        m = m_by_id.get(s.id)
        u = s.utilization
        sample.append({
            "hostname": s.hostname, "role": s.role, "os": s.os, "env": s.env,
            "criticality": s.business_criticality,
            "cpu_mem": f"{s.cpu_cores}c/{s.mem_gb}g",
            "util_max": round(max(u.cpu_p95 or 0, u.mem_p95 or 0, u.disk_used_pct or 0), 0),
            "target": (m.target.product + " " + m.target.spec) if m and m.target else None,
            "apps": s.app_ids,
        })
    payload = {
        "wave": {"name": wave.name, "stage": wave.stage,
                 "depends_on": list(wave.depends_on or []), "rationale": wave.rationale},
        "risk_basis": {"score": basis["score"], "level": basis["level"],
                       "factors": basis["factors"], "signals": basis["signals"]},
        "members_sample": sample, "member_count": len(members),
    }
    return ("Produce the go/no-go + runbook for this migration wave, given its "
            f"context (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "Output ONLY the assessment JSON object.")


REVIEW_SYSTEM = """You are a red-team migration architect reviewing a proposed wave plan for
SEMANTIC problems. Structural validity (no duplicate servers, no dependency
cycles, all depends_on resolve) is ALREADY checked — do NOT report those.
Find the issues rules can't:

  sequencing      : LZ must finish before Data, Data before Apps, Apps before
                   Cutover. Flag a wave that depends on a later-stage wave, or
                   a cutover that doesn't depend on its apps.
  env_mixing     : prod and non-prod (dev/staging) in the SAME wave — risky
                   (blast radius, change window mismatch). Flag it.
  criticality     : high-criticality servers buried in a late/large app wave
                   with no special handling, or a cutover mixing high+low crit.
  blast_radius    : a single wave carrying far more servers than the others
                   (e.g. >3x the median) — too big to roll back cleanly.
  dep_order       : an app wave that doesn't depend on the data wave its DB
                   belongs to, or a frontend cutover that doesn't depend on its
                   backend app wave.
  retain_leak     : an app marked retain/retire that is still depended on by a
                   migrating app (the migrating app will break) — flag the edge.
  coverage        : servers in no wave (the context lists member_count per wave
                   vs the estate total).

Be concrete: name the wave(s) and the specific server/app/env involved. Do NOT
invent issues to seem thorough — if the plan is sound, say so (overall=sound,
findings=[]).

Output ONLY a JSON object, no markdown fences:
  {"overall": "sound" | "needs-work" | "risky",
   "findings": [
     {"severity": "high" | "medium" | "low",
      "wave": "<wave name or id>",
      "issue": "<what's wrong>",
      "suggestion": "<concrete fix>"}, ...],
   "summary": "<2-3 sentences>"}"""


ASK_CLASSIFY_SYSTEM = """You classify a migration operator's natural-language question into ONE intent + its
params, so a deterministic engine can compute the answer (the LLM never does graph
traversal or counting itself). Pick from these intents:

  deps_of          {"app_id"}     "what does <app> depend on?" / "<app> 的依赖"
  dependents_of     {"app_id"}     "what depends on <app>?" / "谁依赖 <app>"
  whatif_delayed    {"app_id"}     "if <app> is delayed/blocked, what's affected?" /
                                   "如果 <app> 延期，影响哪些"
  wave_for_app      {"app_id"}     "which wave is <app> in?"
  riskiest_waves    {"n": int}     "which are the riskiest waves?" (n default 5)
  aggregate         {"scope","metric"}  scope: all|cutover|stage:<x>; metric:
                                   env|criticality|role|target. "how many prod
                                   servers are in cutover?" -> scope=cutover,metric=env
  retain_retire     {}             "which apps are marked retain/retire?"
  search            {"q"}          free-text server search (hostname/role/env/app)
  unknown           {}             can't map to any of the above

Extract app_id from the question if an app id is named. If the question names an
app by a vague description with no id, use unknown (the engine can't resolve names
to ids without an id). Output ONLY JSON, no prose:
  {"intent": "<one of the intents>", "params": {...}}"""


def _ask_classify_context(question: str, servers: List[Server], waves: List[Wave],
                           workloads: List[Workload],
                           strategies: Dict[str, Any]) -> str:
    """Compact estate summary for the classifier — counts + a sample of app_ids
    (so it can extract app_id from the question) + stage list."""
    apps = sorted({w.app_id for w in workloads})
    stages = sorted({w.stage for w in waves})
    strat_count = len(strategies or {})
    payload = {
        "question": question,
        "estate_summary": {
            "servers": len(servers), "waves": len(waves),
            "apps": len(apps), "app_id_sample": apps[:60],
            "wave_stages": stages, "strategies_count": strat_count,
        },
    }
    return (f"Classify this question into an intent + params (JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "Output ONLY the classification JSON object.")


ASK_NARRATE_SYSTEM = """You answer a migration operator's question from a DETERMINISTIC query result
(computed by a graph/aggregate engine — treat it as ground truth). Rules:
- Use ONLY the data in the result. NEVER invent counts, app ids, wave names, or
  hostnames not present in the result.
- Cite wave names / app ids / hostnames from the result when relevant.
- 2-5 sentences, concise, no preamble, no markdown headers.
- If the result intent is "unknown", say you couldn't map the question to an
  estate query and briefly say what kinds of questions you CAN answer (deps,
  what-if, riskiest waves, aggregates, retain/retire, server search)."""


def _review_context(waves: List[Wave], servers: List[Server],
                    workloads: List[Workload], matches: List[Match],
                    profiles: List[CodeProfile],
                    strategies: Optional[Dict[str, "AppStrategy"]]) -> str:
    """Compact plan context for the red-team reviewer — per-wave rollup
    (sizes/env/crit/targets/deps) + app dependency edges + retain/retire apps."""
    by_id = {s.id: s for s in servers}
    m_by_id = {m.server_id: m for m in (matches or [])}
    widx = {w.app_id: w for w in (workloads or [])}
    pidx = {p.app_id: p for p in (profiles or [])}

    def env_crit_of(sids):
        envs: Dict[str, int] = {}
        crit: Dict[str, int] = {}
        prods: Dict[str, int] = {}
        for sid in sids:
            s = by_id.get(sid)
            if not s:
                continue
            if s.env:
                envs[s.env] = envs.get(s.env, 0) + 1
            c = (s.business_criticality or "").lower()
            if c:
                crit[c] = crit.get(c, 0) + 1
            m = m_by_id.get(sid)
            if m and m.target and m.target.product:
                prods[m.target.product] = prods.get(m.target.product, 0) + 1
        return envs, crit, prods

    wave_rows = []
    placed: Set[str] = set()
    for w in waves:
        sids = list(w.server_ids or [])
        placed.update(sids)
        envs, crit, prods = env_crit_of(sids)
        apps = set()
        for sid in sids:
            s = by_id.get(sid)
            if s:
                apps.update(s.app_ids or [])
        wave_rows.append({
            "name": w.name, "id": w.id, "stage": w.stage,
            "n": len(sids), "envs": envs, "criticality": crit,
            "target_products": prods, "depends_on": list(w.depends_on or []),
            "apps": sorted(apps)[:15],
        })
    # app dependency edges (only for apps present in the plan)
    dep_edges = []
    for w in workloads or []:
        for d in (w.depends_on or []):
            if d in widx and w.app_id in widx:
                dep_edges.append({"app": w.app_id, "depends_on": d})
    retain_apps = sorted({a for a, s in (strategies or {}).items()
                          if s.strategy in ("retain", "retire")})
    # legacy retain/retire on profiles (no overlay)
    if not strategies:
        retain_apps = sorted({p.app_id for p in profiles
                              if p.migration_pattern in ("retain", "retire")})
    payload = {
        "waves": wave_rows, "wave_count": len(waves),
        "estate_total": len(servers), "placed_total": len(placed),
        "unplaced_total": len(servers) - len(placed),
        "app_dependency_edges": dep_edges[:200],
        "retain_retire_apps": retain_apps[:200],
    }
    return ("Red-team this migration wave plan for semantic issues. "
            f"Context (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            "Output ONLY the review JSON object.")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort pull the first balanced {...} JSON object out of an LLM
    reply (tolerates ```json fences and surrounding prose)."""
    if not text or text == UNAVAILABLE:
        return None
    t = text.strip()
    import re
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
    if fenced:
        t = fenced.group(1)
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class LLMClient:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.base = self.settings.llm_base.rstrip("/")
        self.model = self.settings.llm_model
        self.timeout = self.settings.llm_timeout

    # -- low level --------------------------------------------------------
    def chat(self, messages: List[Dict[str, str]], stream: bool = False,
             timeout: Optional[int] = None) -> Any:
        if not self.settings.llm_enabled:
            return UNAVAILABLE
        payload = {"model": self.model, "messages": messages, "stream": stream}
        if stream:
            return self._stream(payload, timeout or self.timeout)
        r = httpx.post(f"{self.base}/api/chat", json=payload,
                       timeout=timeout or self.timeout)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")

    def _stream(self, payload: Dict[str, Any], timeout: int):
        with httpx.stream("POST", f"{self.base}/api/chat", json=payload,
                          timeout=timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("done"):
                    return
                piece = (obj.get("message") or {}).get("content")
                if piece:
                    yield piece

    def _safe(self, fn, fallback: str) -> str:
        if not self.settings.llm_enabled:
            return UNAVAILABLE
        try:
            return fn()
        except Exception as e:
            return f"{fallback} (MigraQ error: {e!r})"

    # -- migration helpers ------------------------------------------------
    def explain_match(self, server: Server, match: Match) -> str:
        return self._safe(lambda: self._explain_match(server, match),
                          fallback=match.rationale)

    def _explain_match(self, server: Server, match: Match) -> str:
        sys = ("You are a senior cloud-migration architect for Tencent Cloud. "
               "Explain a server→target mapping in 3-5 short sentences for a "
               "migration lead. Be concrete: name the Tencent product, the "
               "sizing rationale, and one risk/alternative. No preamble.")
        user = (
            f"Source server:\n{json.dumps(_server_brief(server), ensure_ascii=False, indent=2)}\n\n"
            f"Proposed target:\n{json.dumps(match.target.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"Rule rationale: {match.rationale}\n"
            f"Confidence: {match.confidence}\n"
            f"Utilization: cpu {server.utilization.cpu_p95}%, "
            f"mem {server.utilization.mem_p95}%, disk {server.utilization.disk_used_pct}%\n"
            "Explain why this target fits and what to watch out for.")
        return self.chat([{"role": "system", "content": sys},
                          {"role": "user", "content": user}]).strip()

    def ask(self, question: str, servers: List[Server],
            matches: Optional[List[Match]] = None, k: int = 30) -> str:
        return self._safe(lambda: self._ask(question, servers, matches, k),
                          fallback="(could not answer — MigraQ unavailable)")

    def _ask(self, question: str, servers: List[Server],
             matches: Optional[List[Match]], k: int) -> str:
        ctx = _retrieve(question, servers, matches, k)
        sys = ("You are an AI assistant for an IDC→Tencent-Cloud migration. "
               "Answer the operator's question using ONLY the provided "
               "inventory context. If the answer isn't in the context, say so. "
               "Cite server hostnames when relevant. Be concise.")
        user = f"Inventory context (JSON, truncated):\n{json.dumps(ctx, ensure_ascii=False)}\n\nQuestion: {question}"
        return self.chat([{"role": "system", "content": sys},
                          {"role": "user", "content": user}]).strip()

    # -- grounded copilot: classify NL -> deterministic query -> narrate -----
    def ask_grounded(self, question: str, servers: List[Server],
                     waves: List[Wave], workloads: List[Workload],
                     matches: List[Match], profiles: List[CodeProfile],
                     strategies: Dict[str, Any]) -> Dict[str, Any]:
        """Grounded estate Q&A. The graph/aggregate answer is computed by the
        deterministic ``estate_query`` engine (no hallucination); the LLM only
        (1) classifies the question into an intent + params and (2) narrates the
        structured result. Two LLM calls.

        Returns {ok, intent, params, result, answer}. On any failure ``ok`` is
        False and ``answer`` carries a fallback message (the old keyword ``ask``
        result, so callers always get *an* answer)."""
        from ..core.query import INTENTS, estate_query
        fallback = {"ok": False, "intent": None, "params": {}, "result": None,
                    "answer": "(could not answer — MigraQ unavailable)"}
        if not self.settings.llm_enabled:
            return fallback
        # call 1: classify the question into an intent + params
        try:
            cls_raw = self.chat([
                {"role": "system", "content": ASK_CLASSIFY_SYSTEM},
                {"role": "user", "content": _ask_classify_context(
                    question, servers, waves, workloads, strategies)},
            ])
        except Exception as e:
            fallback["answer"] = f"(classify failed: {e!r})"
            return fallback
        cls = _extract_json(cls_raw) or {}
        intent = str(cls.get("intent", "unknown")).strip().lower()
        if intent not in INTENTS:
            intent = "unknown"
        params = cls.get("params") or {}
        # deterministic query (the authoritative, reproducible answer)
        result = estate_query(intent, params, servers=servers, waves=waves,
                              workloads=workloads, matches=matches,
                              profiles=profiles, strategies=strategies or {})
        # call 2: narrate the structured result into a grounded answer
        try:
            ans = self.chat([
                {"role": "system", "content": ASK_NARRATE_SYSTEM},
                {"role": "user", "content":
                 f"Question: {question}\n\nDeterministic query result (JSON):\n"
                 f"{json.dumps(result, ensure_ascii=False, indent=2)}"},
            ]).strip()
        except Exception as e:
            return {"ok": False, "intent": intent, "params": params,
                    "result": result, "answer": f"(narrate failed: {e!r}) "
                    f"Raw result: {json.dumps(result, ensure_ascii=False)[:400]}"}
        return {"ok": True, "intent": intent, "params": params,
                "result": result, "answer": ans or "(empty answer)"}

    def summarize_wave(self, wave: Wave, servers: List[Server],
                       matches: List[Match]) -> str:
        return self._safe(lambda: self._summarize_wave(wave, servers, matches),
                          fallback="(wave summary unavailable — MigraQ error)")

    def _summarize_wave(self, wave: Wave, servers: List[Server],
                        matches: List[Match]) -> str:
        by_id = {s.id: s for s in servers}
        m_by_id = {m.server_id: m for m in matches}
        members = [by_id[sid] for sid in wave.server_ids if sid in by_id]
        rows = [_server_brief(s) | {"target": (m_by_id.get(s.id).target.to_dict()
                                               if m_by_id.get(s.id) else None)}
                for s in members]
        sys = ("You are a migration wave lead. Write a 4-7 sentence exec brief "
               "for the wave: what moves, target products, sequence/dependency, "
               "and the top risk. No preamble, no markdown headers.")
        user = (f"Wave: {wave.name} (stage {wave.stage}, depends_on {wave.depends_on})\n"
                f"Rationale: {wave.rationale}\n"
                f"Members:\n{json.dumps(rows, ensure_ascii=False, indent=2)}")
        return self.chat([{"role": "system", "content": sys},
                          {"role": "user", "content": user}]).strip()

    # -- wave risk assessment + runbook (deterministic score + AI narrative) -
    def assess_wave(self, wave: Wave, servers: List[Server],
                    matches: List[Match], profiles: Optional[List[CodeProfile]] = None
                    ) -> Dict[str, Any]:
        """Assess a migration wave: deterministic risk score/level/factors
        (computed locally, NOT from the LLM — reproducible/auditable) + an
        AI-generated go/no-go, runbook (pre_checks / cutover / rollback), and
        summary, grounded in the wave's members + targets + code profiles + the
        deterministic risk basis.

        Returns {ok, risk_score, risk_level, risk_factors, signals, go_no_go,
        runbook, summary, raw}. On LLM failure ``ok`` is False but the
        deterministic risk_score/level/factors are still returned so callers
        aren't left empty-handed.
        """
        from ..core.codeintel import wave_risk_basis
        basis = wave_risk_basis(wave, servers, matches, profiles or [])
        result: Dict[str, Any] = {
            "ok": False, "risk_score": basis["score"], "risk_level": basis["level"],
            "risk_factors": basis["factors"], "signals": basis["signals"],
            "go_no_go": None, "runbook": None, "summary": "",
        }
        if not self.settings.llm_enabled:
            result["error"] = "MigraQ disabled (risk score still returned)"
            return result
        try:
            raw = self.chat([
                {"role": "system", "content": ASSESS_WAVE_SYSTEM},
                {"role": "user", "content": _assess_wave_context(wave, servers, matches, basis)},
            ])
        except Exception as e:
            result["error"] = f"MigraQ error: {e!r}"
            return result
        obj = _extract_json(raw)
        if not obj:
            result["error"] = "MigraQ output not valid JSON"
            result["raw"] = raw[:800]
            return result
        gng = str(obj.get("go_no_go", "")).strip().lower()
        if gng not in ("go", "hold", "no-go"):
            gng = "hold"
        result.update({
            "ok": True, "go_no_go": gng,
            "runbook": {
                "pre_checks": [str(x) for x in (obj.get("pre_checks") or [])][:30],
                "cutover": [str(x) for x in (obj.get("cutover") or [])][:30],
                "rollback": [str(x) for x in (obj.get("rollback") or [])][:30],
            },
            "summary": str(obj.get("summary", "")).strip(),
            "raw": raw,
        })
        return result

    # -- adversarial plan review (red-team the wave plan) -------------------
    def review_plan(self, waves: List[Wave], servers: List[Server],
                    workloads: List[Workload], matches: List[Match],
                    profiles: Optional[List[CodeProfile]] = None,
                    strategies: Optional[Dict[str, "AppStrategy"]] = None
                    ) -> Dict[str, Any]:
        """Red-team a wave plan for SEMANTIC issues the structural
        ``validate_plan`` can't catch (it only checks dups/cycles/unknown
        refs). Returns {ok, overall (sound/needs-work/risky), findings:
        [{severity, wave, issue, suggestion}], summary}. Advisory only.

        Grounded in a compact per-wave context (sizes, env/criticality mix,
        target products, depends_on, app sample) + retain/retire apps. The
        auditor looks for: bad sequencing (LZ->Data->Apps->Cutover), env
        mixing, criticality placement, blast radius (over-sized waves),
        dependency-order violations, and retain/retire apps still depended on
        by migrating apps.
        """
        if not self.settings.llm_enabled:
            return {"ok": False, "overall": None, "findings": [],
                    "error": "MigraQ disabled"}
        try:
            raw = self.chat([
                {"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": _review_context(
                    waves, servers, workloads, matches, profiles or [], strategies)},
            ])
        except Exception as e:
            return {"ok": False, "overall": None, "findings": [],
                    "error": f"MigraQ error: {e!r}"}
        obj = _extract_json(raw)
        if not obj:
            return {"ok": False, "overall": None, "findings": [],
                    "error": "MigraQ output not valid JSON", "raw": raw[:800]}
        overall = str(obj.get("overall", "")).strip().lower()
        if overall not in ("sound", "needs-work", "risky"):
            overall = "needs-work"
        findings = []
        for f in (obj.get("findings") or []):
            findings.append({
                "severity": str(f.get("severity", "medium")).strip().lower() or "medium",
                "wave": str(f.get("wave", "")).strip(),
                "issue": str(f.get("issue", "")).strip(),
                "suggestion": str(f.get("suggestion", "")).strip(),
            })
        return {"ok": True, "overall": overall, "findings": findings[:50],
                "summary": str(obj.get("summary", "")).strip(), "raw": raw}

    def right_size(self, server: Server, match: Match) -> str:
        return self._safe(lambda: self._right_size(server, match),
                          fallback="(right-size unavailable — MigraQ error)")

    def _right_size(self, server: Server, match: Match) -> str:
        sys = ("You are a capacity planner. Given observed utilization and the "
               "proposed Tencent Cloud spec, confirm or adjust the sizing in "
               "2-4 sentences. Prefer right-sizing over headroom >2x.")
        user = (f"Server: {_server_brief(server)}\n"
                f"Utilization: cpu {server.utilization.cpu_p95}%, "
                f"mem {server.utilization.mem_p95}%, disk {server.utilization.disk_used_pct}%\n"
                f"Proposed: {match.target.product} {match.target.spec}")
        return self.chat([{"role": "system", "content": sys},
                          {"role": "user", "content": user}]).strip()

    # -- match audit (AI second-opinion on the rule-based target) -----------
    def audit_match(self, server: Server, match: Match,
                    profile: Optional[CodeProfile] = None) -> Dict[str, Any]:
        """Critique the rule-based migration target for a server.

        Returns {ok, verdict, confidence, critique, alternative_target,
        alternative_spec, rationale, risks}. ``verdict`` is one of:
          keep    — the rule target is right
          change  — suggest alternative_target (e.g. rule said CVM but a stateful
                    DB role should be CDB)
          review  — uncertain / needs a human (e.g. mixed signals)
        On any failure ``ok`` is False and ``verdict`` is "" — callers fall back
        to the rule match. Advisory only; never changes the stored Match.
        """
        if not self.settings.llm_enabled:
            return {"ok": False, "verdict": "", "error": "MigraQ disabled"}
        try:
            raw = self.chat([
                {"role": "system", "content": AUDIT_SYSTEM},
                {"role": "user", "content": _audit_context(server, match, profile)},
            ])
        except Exception as e:
            return {"ok": False, "verdict": "", "error": f"MigraQ error: {e!r}", "raw": ""}
        obj = _extract_json(raw)
        if not obj:
            return {"ok": False, "verdict": "", "error": "MigraQ output not valid JSON",
                    "raw": raw[:800]}
        v = str(obj.get("verdict", "")).strip().lower()
        if v not in ("keep", "change", "review"):
            return {"ok": False, "verdict": "", "error": f"invalid verdict {v!r}",
                    "raw": raw[:800]}
        return {
            "ok": True, "verdict": v,
            "confidence": float(obj.get("confidence", 0.5) or 0.5),
            "critique": str(obj.get("critique", "")).strip(),
            "alternative_target": str(obj.get("alternative_target", "") or "").strip() or None,
            "alternative_spec": str(obj.get("alternative_spec", "") or "").strip() or None,
            "rationale": str(obj.get("rationale", "")).strip(),
            "risks": [str(x) for x in (obj.get("risks") or [])][:20],
            "raw": raw,
        }

    # -- 7R strategy (after reconsolidate) -------------------------------
    def seven_r_strategy(self, app_id: str, servers: List[Server],
                         workloads: List[Workload], matches: List[Match],
                         profiles: Optional[List[CodeProfile]] = None
                         ) -> Dict[str, Any]:
        """Assign a 7R migration strategy to one app via the LLM.

        Returns a dict: {ok, app_id, strategy, rationale, target, confidence,
        effort, key_changes, raw}. On any failure (LLM down / unparseable /
        invalid strategy) ``ok`` is False and ``strategy`` is "" — callers
        fall back to the executor-pushed ``CodeProfile.migration_pattern``.
        """
        if not self.settings.llm_enabled:
            return {"ok": False, "app_id": app_id, "strategy": "",
                    "error": "MigraQ disabled"}
        ctx = _seven_r_context(app_id, servers, workloads, matches, profiles or [])
        try:
            raw = self.chat([{"role": "system", "content": SEVEN_R_SYSTEM},
                             {"role": "user", "content": ctx}])
        except Exception as e:
            return {"ok": False, "app_id": app_id, "strategy": "",
                    "error": f"MigraQ error: {e!r}", "raw": ""}
        obj = _extract_json(raw)
        if not obj:
            return {"ok": False, "app_id": app_id, "strategy": "",
                    "error": "MigraQ output was not valid JSON", "raw": raw[:800]}
        strat = str(obj.get("strategy", "")).strip().lower()
        if strat not in ALL_PATTERNS_7R:
            return {"ok": False, "app_id": app_id, "strategy": "",
                    "error": f"invalid strategy {strat!r} (not one of 7R)",
                    "raw": raw[:800]}
        return {
            "ok": True, "app_id": app_id, "strategy": strat,
            "rationale": str(obj.get("rationale", "")).strip(),
            "target": str(obj.get("target", "")).strip(),
            "confidence": float(obj.get("confidence", 0.5) or 0.5),
            "effort": str(obj.get("effort", "medium")).strip().lower() or "medium",
            "key_changes": [str(x) for x in (obj.get("key_changes") or [])][:20],
            "raw": raw,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _server_brief(s: Server) -> Dict[str, Any]:
    return {
        "hostname": s.hostname, "fqdn": s.fqdn, "role": s.role,
        "source_type": s.source_type, "os": s.os, "os_version": s.os_version,
        "cpu_cores": s.cpu_cores, "mem_gb": s.mem_gb,
        "disk_gb": s.total_disk_gb, "env": s.env,
        "criticality": s.business_criticality, "tags": s.tags,
        "app_ids": s.app_ids,
        "utilization": s.utilization.to_dict(),
    }


def _seven_r_context(app_id: str, servers: List[Server], workloads: List[Workload],
                     matches: List[Match], profiles: List[CodeProfile]) -> str:
    """Compact per-app context for the 7R strategist (JSON string).

    Joins the app's servers, its workload/dependency edges, its matched Tencent
    targets, and its code profile (readiness / blockers / executor's pattern)
    so the LLM has everything reconsolidate produced, without dumping 15K rows.
    """
    m_by_id = {m.server_id: m for m in matches}
    pidx = {p.app_id: p for p in profiles}
    w = next((x for x in workloads if x.app_id == app_id), None)
    # an app's servers: those carrying the app_id, else the workload's members
    srvs = [s for s in servers if app_id in (s.app_ids or [])]
    if not srvs and w is not None:
        srvs = [s for s in servers if s.id in set(w.server_ids)]
    rows = []
    for s in srvs[:40]:
        m = m_by_id.get(s.id)
        rows.append({
            "hostname": s.hostname, "role": s.role, "os": s.os,
            "cpu": s.cpu_cores, "mem_gb": s.mem_gb, "env": s.env,
            "criticality": s.business_criticality,
            "target": (m.target.product + " " + m.target.spec) if m else None,
            "match_confidence": m.confidence if m else None,
            "utilization": s.utilization.to_dict(),
        })
    workload = None
    if w is not None:
        workload = {"app_id": w.app_id, "name": w.name, "tier": w.tier,
                    "env": w.env, "server_count": len(w.server_ids),
                    "depends_on": w.depends_on}
    prof = pidx.get(app_id)
    code = None
    if prof is not None:
        cats = {}
        for f in (prof.findings or []):
            cats[f.category] = cats.get(f.category, 0) + 1
        code = {
            "language": prof.language, "runtime": prof.runtime,
            "framework": prof.framework,
            "cloud_readiness": prof.cloud_readiness,
            "executor_pattern": prof.migration_pattern,   # the executor's prior call
            "refactor_effort": prof.refactor_effort,
            "blockers": prof.blockers,
            "code_deps": prof.code_deps,
            "finding_categories": cats,
            "summary": prof.summary,
        }
    payload = {"app_id": app_id, "workload": workload,
               "servers_sample": rows, "server_count": len(srvs),
               "code_profile": code}
    return (f"Assign the 7R migration strategy for this app, given its "
            f"reconsolidated context (JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            f"Output ONLY the strategy JSON object.")


def _retrieve(question: str, servers: List[Server],
              matches: Optional[List[Match]], k: int) -> List[Dict[str, Any]]:
    """Trivial keyword retrieval — no embeddings dependency.

    Scores each server by keyword overlap with the question (role/os/tags/
    hostname/app). Good enough for a copilot; swap for a vector store later.
    """
    q = question.lower()
    stop = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "for",
            "what", "which", "how", "is", "are", "do", "does", "my", "our",
            "servers", "server", "run", "running", "all", "list", "show", "?"}
    qterms = [w for w in q.replace("?", " ").split() if w and w not in stop]
    m_by_id = {m.server_id: m for m in (matches or [])}

    def score(s: Server) -> int:
        hay = " ".join([s.hostname, s.fqdn, s.role, s.os, s.env,
                        s.business_criticality, " ".join(s.tags),
                        " ".join(s.app_ids), s.source_type]).lower()
        return sum(hay.count(t) for t in qterms) + (5 if s.role and s.role in q else 0)

    ranked = sorted(servers, key=score, reverse=True)[:k]
    out = []
    for s in ranked:
        d = _server_brief(s)
        if m_by_id.get(s.id):
            d["target"] = m_by_id[s.id].target.to_dict()
        out.append(d)
    return out


def get_client(settings: Optional[Settings] = None) -> LLMClient:
    return LLMClient(settings)