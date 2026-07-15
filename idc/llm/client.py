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
from ..core.models import ALL_PATTERNS_7R, STRATEGY_CHOICES, CodeProfile, Match, Server, Wave, Workload

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
  retire            decommission — unused, redundant, sunset. NOT a migration
                    target.
  relocate          move to a different platform/location (e.g. on-prem hypervisor
                    -> cloud IaaS) with NO schema or code changes — same app, new
                    hosting. Use when the app is portable as-is but the hosting
                    must move (licensing, data-sovereignty, exit a DC).
  retain            keep on-prem — regulated data, mainframe, hard latency tie to
                    on-prem, or not worth migrating. NOT a migration target. This
                    is a keep-on-prem disposition (not one of the seven Rs), but
                    you may return it when the app genuinely should not migrate.

Decision guidance:
- Stateless + cloud-ready + low effort -> rehost or rehost-container.
- Stateless + elastic + container-friendly runtime -> rehost-container (TKE).
- Stateful DB/cache -> replatform to managed (CDB / TencentDB for Redis).
- Portable as-is but hosting must move (no code/schema change) -> relocate.
- Hard blockers / legacy runtime / low cloud readiness -> refactor, repurchase,
  retire, or retain depending on business value.
- Never invent a strategy outside the list above.

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
- Read the server's per-partition disk + MOUNT layout (the signal the rule
  engine used): Oracle OFA mounts (oradata/redolog/fra → Oracle DB → TData/CDB),
  /zpaas (self-hosted PaaS → TKE), /data + db-engine tags (→ TencentDB), shared
  NFS mounts (→ CFS), object-store mounts (→ COS), hadoop mounts (→ EMR). Also
  read the OS-EOL + warranty buckets (an EOL/expired-OS host may need rehost-
  container or a newer base image regardless of target) and the app's code
  profile (runtime/framework/blockers).
- If the rule target matches the server's role + mount layout + workload shape
  + code profile, verdict = "keep" (don't second-guess a correct call).
- If a clearly better target exists (e.g. db-role with Oracle/data mounts
  mapped to CVM should be CDB/TData; stateless container-friendly app mapped to
  CVM could be TKE; hadoop mounts mapped to CVM should be EMR), verdict =
  "change" and name alternative_target (+spec).
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
        payload["code_profile"] = _code_profile_brief(profile)
    return ("Audit the rule-based migration target for this server, given its "
            f"context (JSON — note the per-partition disk + mount layout, the "
            f"OS-EOL / warranty buckets, and the code profile):\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
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
- For a "search" result, "count" is the TRUE total matches and "shown" is how
  many hostnames are listed (capped at 30). Report the true total; if
  "truncated" is true, say you're showing the first 30 and the operator can
  narrow the query to see more. Never claim the listed hostnames are all of them.
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
    reply (tolerates ```json fences and surrounding prose, and braces that
    appear inside string values)."""
    if not text or text == UNAVAILABLE:
        return None
    import re
    t = text.strip()
    # 1) the common case: the whole reply (or the fenced block) is the object.
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.DOTALL)
    candidates = [t, fenced.group(1)] if fenced else [t]
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
    # 2) brace counting that skips string literals, so a "}" inside a string
    # value (e.g. "rule said CVM} wrong") doesn't prematurely close the object.
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
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
    def explain_match(self, server: Server, match: Match,
                      profile: Optional["CodeProfile"] = None) -> str:
        return self._safe(lambda: self._explain_match(server, match, profile),
                          fallback=match.rationale)

    def _explain_match(self, server: Server, match: Match,
                       profile: Optional["CodeProfile"] = None) -> str:
        sys = ("You are a senior cloud-migration architect for Tencent Cloud. "
               "Explain a server→target mapping in 3-5 short sentences for a "
               "migration lead. Be concrete: name the Tencent product, the "
               "sizing rationale, and one risk/alternative. Read the server's "
               "per-partition disk + MOUNT layout (Oracle OFA /oradata/redolog, "
               "/zpaas, /data dirs, shared NFS), its OS-EOL + warranty buckets, "
               "its tags/role, and the app's code profile (if any) — tie the "
               "target choice to THOSE signals, not just role. No preamble.")
        payload = {"server": _server_brief(server),
                   "code_profile": _code_profile_brief(profile)}
        user = (
            f"Source server + code profile:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            f"Proposed target:\n{json.dumps(match.target.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"Rule rationale: {match.rationale}\n"
            f"Confidence: {match.confidence}\n"
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

    def right_size(self, server: Server, match: Match,
                   profile: Optional["CodeProfile"] = None) -> str:
        return self._safe(lambda: self._right_size(server, match, profile),
                          fallback="(right-size unavailable — MigraQ error)")

    def _right_size(self, server: Server, match: Match,
                    profile: Optional["CodeProfile"] = None) -> str:
        sys = ("You are a capacity planner. Given the server's per-partition "
               "disk + mount layout, observed utilization, the proposed Tencent "
               "Cloud spec, and the app's code profile (if any), confirm or "
               "adjust the sizing in 2-4 sentences. Reason from the actual disk "
               "layout (which mounts are large / used) and util p95s, not just "
               "totals. Prefer right-sizing over headroom >2x, but never under-"
               "size a stateful DB's data volume or a host on an EOL/expired-"
               "warranty OS that must move first.")
        payload = {"server": _server_brief(server),
                   "code_profile": _code_profile_brief(profile)}
        user = (f"Server + code profile:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
                f"Proposed: {match.target.product} {match.target.spec}\n"
                f"Confidence: {match.confidence}")
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
        if strat not in STRATEGY_CHOICES:
            return {"ok": False, "app_id": app_id, "strategy": "",
                    "error": f"invalid strategy {strat!r} (not one of the 7R / retain)",
                    "raw": raw[:800]}
        # F2 — clamp the LLM-emitted confidence with a deterministic ceiling
        # built from the executor's own signals + data-coverage. The LLM may
        # express LESS confidence (nuance the formula can't see) but never more
        # than the signals warrant; an over-confident model can't stamp 0.9 on
        # a thinly-known app with hard blockers.
        from ..core.codeintel import seven_r_confidence
        from ..core.match import assessment_confidence_of
        prof = next((p for p in (profiles or []) if p.app_id == app_id), None)
        srvs = [s for s in servers if app_id in (s.app_ids or [])]
        if not srvs:
            w = next((x for x in workloads if x.app_id == app_id), None)
            if w is not None:
                srvs = [s for s in servers if s.id in set(w.server_ids)]
        data_cov = (sum(assessment_confidence_of(s, has_code_profile=prof is not None)
                        for s in srvs) / len(srvs)) if srvs else 0.0
        # No code profile → no code-level risk is KNOWN, so use a neutral-
        # optimistic base (0.7, low effort, no blockers) for the ceiling.
        # Using 0.5 here would systematically depress confidence for COTS /
        # packaged apps (the *easiest* rehosts) below a complex refactor that
        # happens to have a profile — the wrong ranking.
        if prof is not None:
            cr, blk, eff = prof.cloud_readiness, len(prof.blockers), prof.refactor_effort
        else:
            cr, blk, eff = 0.7, 0, "low"
        ceiling = seven_r_confidence(
            cloud_readiness=cr, blocker_count=blk,
            refactor_effort=eff, data_coverage=data_cov)
        llm_conf = float(obj.get("confidence", 0.5) or 0.5)
        confidence = max(0.05, min(llm_conf, ceiling))
        clamped = llm_conf > ceiling + 1e-9
        return {
            "ok": True, "app_id": app_id, "strategy": strat,
            "rationale": str(obj.get("rationale", "")).strip(),
            "target": str(obj.get("target", "")).strip(),
            "confidence": confidence,
            "confidence_ceiling": round(ceiling, 3),
            "confidence_clamped": clamped,
            "effort": str(obj.get("effort", "medium")).strip().lower() or "medium",
            "key_changes": [str(x) for x in (obj.get("key_changes") or [])][:20],
            "raw": raw,
        }

    # -- LLM-driven LZ design interview (Phase B) -----------------------------
    def design_lz(self, demand: str, servers: List[Server],
                  matches: Optional[List[Match]] = None,
                  onprem_cidrs: Optional[List[str]] = None,
                  conversation: Optional[List[Dict[str, str]]] = None,
                  max_rounds: int = 3,
                  scale: Optional[str] = None) -> Dict[str, Any]:
        """The LLM LZ design interview (Phase B). The LLM proposes a blueprint
        dict; the deterministic ``validate_lz_design`` is the authority and the
        LLM is looped until the proposal passes (CIDR non-overlap, symmetric
        peering, required keys) — the same propose→validate→loop shape as the
        wave planner's cap revision. The LLM NEVER emits Terraform; the
        executor's ``iac-emit`` still emits HCL from the blueprint.

        ``demand`` is the operator's message for THIS turn: the opening
        requirement on the first call, a follow-up ("add a pci tier", "make the
        hub CIDR 10.8/16") on continuation. ``conversation`` is the prior
        multi-turn chat history (None/[] on the first call); it is extended with
        the new turn(s) and carried on the returned design so the UI can persist
        it and continue the interview (iterate).

        ``scale`` (small/medium/large) sets the DEFAULT STRATEGY the LLM should
        honor — the tier's comparison matrix (account/networking/security/audit/
        budgeting) + topology shape + archetype-count bounds + the cross-scale
        golden rules. Inferred from the estate when None (``lz.scale_for``).
        The operator-customizable parts (app placement, CIDRs, archetype names,
        extra compliance tiers) come from ``demand`` (the AI prompt); everything
        else is the scale default, auto-filled into ``requirements.governance``
        so the design records which scale it was built for.

        Returns ``{ok, design: LZDesign|None, validation, rounds, raw, error}``.
        ``ok=True`` once the LLM produces a blueprint that passes
        ``validate_lz_design``. On LLM-disabled / failure, ``ok`` is False,
        ``design`` is the built-in default (a strict superset — callers keep
        working) or the last-attempted (invalid) design with the conversation
        preserved, and ``error`` carries the reason. ``rounds`` records each
        attempt's validation errors for the UI.
        """
        from ..core.lz import (validate_lz_design, default_lz_design,
                               default_lz_design_for_scale, scale_for,
                               LZ_SCALE_TIERS, _governance_for_scale)
        from ..core.models import LZDesign, _new_id

        def _bad(error: str, design=None, rounds=None, raw="") -> Dict[str, Any]:
            return {"ok": False, "design": design or default_lz_design(),
                    "validation": None, "rounds": rounds or [],
                    "raw": raw, "error": error}

        if not self.settings.llm_enabled:
            return _bad("MigraQ disabled — returned the built-in default design")
        # the scale tier drives the default strategy; infer from the estate when
        # the caller didn't pin one. Baked into the system prompt + the estate
        # context so the LLM's proposal matches the estate's maturity, and
        # stamped on the returned design so the gate/UI know which scale it serves.
        tier = scale_for(servers, scale_override=scale)
        conv = [dict(t) for t in (conversation or [])]
        first_turn = not conv
        user_content = (
            (_lz_estate_context(servers, matches, onprem_cidrs, tier)
             + "\n\nOperator demand:\n" + demand)
            if first_turn else demand)
        conv.append({"role": "user", "content": user_content})

        rounds: List[Dict[str, Any]] = []
        raw = ""
        last_design: Optional[LZDesign] = None
        last_validation: Optional[Dict[str, Any]] = None
        for attempt in range(1, max_rounds + 1):
            try:
                raw = self.chat([{"role": "system",
                                  "content": _lz_design_system(tier)},
                                 *conv])
            except Exception as e:
                return _bad(f"MigraQ error: {e!r}", design=last_design,
                            rounds=rounds, raw=raw)
            if not raw or raw == UNAVAILABLE:
                return _bad("MigraQ unavailable", design=last_design,
                            rounds=rounds, raw=raw or UNAVAILABLE)
            conv.append({"role": "assistant", "content": raw})  # record the reply
            obj = _extract_json(raw)
            if obj is None:
                rounds.append({"attempt": attempt, "ok": False,
                               "errors": ["output was not valid JSON"],
                               "raw_head": raw[:400]})
                conv.append({"role": "user", "content":
                             "Your previous output was not valid JSON. Output "
                             "ONLY the design JSON object, no prose, no fences."})
                continue
            # Merge the operator-supplied onprem_cidrs (ground truth) FIRST, then
            # any extra the LLM proposes — operator's must always be present. The
            # old ``obj.get(...) or onprem_cidrs`` let the LLM narrow/drop the
            # operator's CIDRs, so a VPC overlapping the real on-prem network
            # could pass validate_lz_design's non-overlap check.
            _llm_cidrs = obj.get("onprem_cidrs")
            if not isinstance(_llm_cidrs, list):
                _llm_cidrs = []
            merged_cidrs = list(dict.fromkeys(
                list(onprem_cidrs or [])
                + [str(c).strip() for c in _llm_cidrs if str(c).strip()]))
            # stamp the scale tier + merge the scale's default governance into
            # requirements (the operator/LLM may override per-key via
            # requirements.governance in the proposal; the scale defaults fill
            # anything left unset so the cross-scale golden rules are recorded).
            req = obj.get("requirements") if isinstance(
                obj.get("requirements"), dict) else {}
            req = dict(req)
            req["scale"] = tier
            req_gov = req.get("governance") if isinstance(
                req.get("governance"), dict) else {}
            base_gov = _governance_for_scale(tier)
            req["governance"] = {**base_gov, **req_gov}
            req.setdefault("defaults_applied", LZ_SCALE_TIERS[tier])
            # the AI description: prefer the top-level ``summary`` the prompt asks
            # for; fall back to ``requirements.notes`` (also AI-authored, "1-2
            # sentences") so the Saved designs column is never empty for an older
            # / terser LLM output.
            _sum = str(obj.get("summary") or "").strip()
            if not _sum:
                _sum = str((obj.get("requirements") or {}).get("notes") or "").strip()
            design = LZDesign(
                design_id=_new_id("lzd"),
                name=str(obj.get("name") or "").strip() or "MigraQ design",
                summary=_sum[:500],
                archetypes=obj.get("archetypes") or {},
                requirements=req,
                conversation=conv,
                onprem_cidrs=merged_cidrs,
                is_active=False,
                scale=tier,
            )
            last_design = design
            last_validation = v = validate_lz_design(design)
            if v["ok"]:
                rounds.append({"attempt": attempt, "ok": True, "errors": [],
                               "raw_head": raw[:400]})
                return {"ok": True, "design": design, "validation": v,
                        "rounds": rounds, "raw": raw, "error": ""}
            rounds.append({"attempt": attempt, "ok": False, "errors": v["errors"],
                           "raw_head": raw[:400]})
            # feed the deterministic errors (+ golden-rule warnings) back to the
            # LLM and loop. Warnings are guidance, not hard rejects, but surfacing
            # them nudges the LLM toward a governance-sound design next round.
            feedback = list(v["errors"])
            if v.get("warnings"):
                feedback += ["WARNING (governance guidance): " + w
                             for w in v["warnings"]]
            conv.append({"role": "user", "content":
                         "The deterministic validator rejected this design:\n- "
                         + "\n- ".join(feedback)
                         + "\n\nFix EVERY error and re-emit the full design JSON "
                         "(same shape). Output ONLY the JSON object."})
        # exhausted the rounds — return the last attempted design (with its
        # conversation) so the UI can show what was tried + the errors, else the
        # built-in default when the LLM never produced parseable JSON.
        if last_design is not None:
            return {"ok": False, "design": last_design, "validation": last_validation,
                    "rounds": rounds, "raw": raw,
                    "error": f"MigraQ did not produce a valid design after "
                             f"{max_rounds} round(s)"}
        return _bad(f"MigraQ did not produce a valid design after {max_rounds} "
                    f"round(s)", rounds=rounds, raw=raw)

    # -- LLM-driven network policy (Phase B+ — optional carving-policy proposal) -
    def design_network_policy(self, demand: str, servers: List[Server],
                              lz_design, matches: Optional[List[Match]] = None,
                              conversation: Optional[List[Dict[str, str]]] = None,
                              max_rounds: int = 2) -> Dict[str, Any]:
        """Propose a network carving POLICY (subnet size, tier grouping) for the
        network designer via the LLM. The deterministic ``build_network_design``
        instantiates + validates it — same propose→validate→loop split as
        ``design_lz``. The policy is small JSON; the LLM never emits VPC/subnet
        IPs (the deterministic engine carves + assigns those, reproducibly) and
        never renames/merges accounts (account naming is Layer 1 — the LZ design;
        the account name IS the archetype name).

        ``demand`` is the operator's message for this turn (e.g. "/24 subnets,
        group db+cache together"). ``lz_design`` is the active LZ design (its
        archetypes are the account/VPC basis). Returns
        ``{ok, policy, rounds, error}`` — ``policy`` is a dict ready for
        ``build_network_policy(policy=...)``; on failure ``ok`` is False and the
        deterministic ``default_network_policy`` is returned so callers keep
        working. Optional: the network designer works fully without this (the
        default policy is the workhorse).
        """
        from ..core.network import default_network_policy, TIER_ORDER

        def _bad(error: str, policy=None, rounds=None, raw="") -> Dict[str, Any]:
            return {"ok": False, "policy": policy or default_network_policy(),
                    "rounds": rounds or [], "raw": raw, "error": error}

        if not self.settings.llm_enabled:
            return _bad("MigraQ disabled — returned the default network policy")
        archs = list((lz_design.archetypes or {}).keys()) if lz_design else []
        if not archs:
            return _bad("no LZ archetypes to design a network for")

        def _validate(pol: Dict[str, Any]) -> List[str]:
            errs: List[str] = []
            pfx = pol.get("subnet_prefix")
            if pfx is not None:
                if not isinstance(pfx, int) or not (8 <= pfx <= 30):
                    errs.append("subnet_prefix must be null or an int 8..30")
            # tier_order: the subnet tiers to carve, in order. CUSTOM names are
            # allowed (the operator may say "public/private/protected" instead of
            # the default web/app/data/infra) — the engine carves one subnet per
            # name regardless of vocabulary. Must be a non-empty list of unique
            # non-empty strings (bounded so a /16 VPC can still hold them all).
            to = pol.get("tier_order")
            to_set = set(to) if isinstance(to, list) else set()
            if not (isinstance(to, list) and 1 <= len(to) <= 8
                    and len(to) == len(to_set)
                    and all(isinstance(t, str) and t.strip()
                            and not t.strip().startswith("to_") for t in to)):
                errs.append("tier_order must be a non-empty list (1..8) of unique "
                            "non-empty tier names (custom names like "
                            "public/private/protected are allowed)")
            # account naming/merging is NOT a network-policy knob (Layer 1 — the
            # LZ design owns accounts; the account name IS the archetype name).
            # A legacy/LLM ``account_map`` is silently ignored by the engine, so
            # don't validate it — just drop it from the policy below.
            # default_tier (optional): the tier for roles not in `tiers`. Must be
            # one of the tier_order names (the engine falls back to it when a
            # role's mapped tier isn't carved — see network.build_network_design).
            dt = pol.get("default_tier")
            if dt is not None and not (isinstance(dt, str) and dt in to_set):
                errs.append(f"default_tier {dt!r} must be one of tier_order "
                            f"{sorted(to_set)}")
            # tiers is a role -> tier override the engine consumes; values MUST be
            # tier_order names or the server lands in a subnet that doesn't exist
            # (the engine then falls back to default_tier, but an explicit bad
            # value still indicates the LLM mis-named a tier).
            tiers = pol.get("tiers") or {}
            if not isinstance(tiers, dict):
                errs.append("tiers must be an object (role -> tier)")
            else:
                bad = [f"{k}->{v}" for k, v in tiers.items()
                       if not (isinstance(v, str) and v in to_set)]
                if bad:
                    errs.append(f"tiers values must be in tier_order "
                                f"{sorted(to_set)}; bad: {bad}")
            return errs

        conv = [dict(t) for t in (conversation or [])]
        first_turn = not conv
        ctx = _lz_estate_context(servers, matches)
        arch_list = ", ".join(archs)
        user_content = (
            (ctx + f"\n\nThe active LZ design has archetypes: {arch_list} "
             "(each is one VPC + account). Operator demand:\n" + demand)
            if first_turn else demand)
        conv.append({"role": "user", "content": user_content})

        rounds: List[Dict[str, Any]] = []
        raw = ""
        last_pol: Optional[Dict[str, Any]] = None
        for attempt in range(1, max_rounds + 1):
            try:
                raw = self.chat([
                    {"role": "system", "content": NETWORK_POLICY_SYSTEM},
                    *conv])
            except Exception as e:
                return _bad(f"MigraQ error: {e!r}", policy=last_pol, rounds=rounds, raw=raw)
            if not raw or raw == UNAVAILABLE:
                return _bad("MigraQ unavailable", policy=last_pol, rounds=rounds, raw=raw)
            conv.append({"role": "assistant", "content": raw})
            obj = _extract_json(raw)
            if obj is None:
                rounds.append({"attempt": attempt, "ok": False,
                               "errors": ["output was not valid JSON"]})
                conv.append({"role": "user", "content":
                             "Output was not valid JSON. Output ONLY the policy JSON."})
                continue
            pol = {
                "subnet_prefix": obj.get("subnet_prefix"),
                "tier_order": obj.get("tier_order") or list(TIER_ORDER),
                "tiers": obj.get("tiers") or {},
                "default_tier": obj.get("default_tier"),
                "notes": str(obj.get("notes", "")).strip(),
            }
            last_pol = pol
            errs = _validate(pol)
            if not errs:
                rounds.append({"attempt": attempt, "ok": True, "errors": []})
                return {"ok": True, "policy": pol, "rounds": rounds, "raw": raw,
                        "error": ""}
            rounds.append({"attempt": attempt, "ok": False, "errors": errs})
            conv.append({"role": "user", "content":
                         "The validator rejected this policy:\n- "
                         + "\n- ".join(errs)
                         + "\n\nFix every error and re-emit ONLY the policy JSON."})
        return _bad(f"MigraQ did not produce a valid policy after {max_rounds} "
                    f"round(s)", policy=last_pol, rounds=rounds, raw=raw)


NETWORK_POLICY_SYSTEM = """You are a Tencent Cloud network architect. Given an estate summary and the
active Landing Zone archetypes, design a NETWORK CARVING POLICY as JSON. You
NEVER emit VPC/subnet CIDRs or host IPs — the deterministic engine carves the
subnets from each archetype's VPC cidr and assigns IPs reproducibly. You only
choose HOW to carve. You do NOT rename or merge accounts — account naming is the
Landing Zone designer's job (each archetype IS one account); the account name is
the archetype name.

Policy shape:
  {"subnet_prefix": <int 8..30 or null>,   // null = auto (sized per tier by demand)
   "tier_order": ["<tier>", ...],          // subnet tiers to carve, order matters
   "tiers": {"<role>": "<tier>", ...},     // role -> tier grouping (optional)
   "default_tier": "<tier>",               // tier for roles not in `tiers`
   "notes": "<1-2 sentence description of this carving policy — the tier layout
             + grouping intent, shown as the design's AI description in the
             Saved network designs table>"}

Rules:
- subnet_prefix is the subnet size (null = let the engine auto-size: each tier
  subnet is sized to its actual host count with ~2x headroom, so a tier with many
  hosts gets a large block and a near-empty tier gets a small one). Only set it
  when the operator names a specific subnet size; a fixed prefix makes every tier
  an equal-sized block, which can starve a big tier.
- tier_order is the list of SUBNET TIERS to carve, one subnet per name, IN ORDER.
  The DEFAULT is ["web","app","data","infra"] (4 tiers: web/frontend->web,
  app/general->app, db/cache/hadoop->data, k8s/monitoring/infra->infra). BUT the
  operator may name tiers differently — e.g. 3 tiers ["public","private",
  "protected"], or ["web","app","data"], or a single ["app"]. When the operator
  names specific tiers, USE EXACTLY THOSE NAMES in tier_order; do not force the
  default 4. 1..8 unique non-empty names.
- tiers maps each estate ROLE to one of the tier_order names. When the operator
  uses custom tier names, map EVERY role to one of those names — do not leave a
  role pointing at a default name that isn't in tier_order. Reconcile any
  grouping the operator gives you into the tiers you declared (e.g. if they say
  3 tiers but list a 4th group, fold it into the closest of the 3).
- default_tier is the tier for any role you did not explicitly map (must be one
  of tier_order). The engine uses it as the fallback so no server goes unplaced.

Output ONLY the JSON object, no prose, no markdown fences."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _server_brief(s: Server) -> Dict[str, Any]:
    """Rich per-host context for the drawer's LLM actions (explain / right-size /
    audit). Carries the FULL picture the rule engine used, not just totals —
    the per-partition disk + *mount* layout (the signal that classifies Oracle
    OFA / zpaas / data dirs → CDB / TKE / CVM), the OS-EOL + warranty buckets
    (the silent-rehost-of-EOL-OS / 脱保 signals), and the network placement."""
    return {
        "hostname": s.hostname, "fqdn": s.fqdn, "role": s.role,
        "source_type": s.source_type, "os": s.os, "os_version": s.os_version,
        "cpu_cores": s.cpu_cores, "mem_gb": s.mem_gb,
        "disk_gb": s.total_disk_gb,
        "disks": [{"label": d.name, "mount": d.fs, "size_gb": d.size_gb}
                  for d in (s.disks or [])],
        "env": s.env, "criticality": s.business_criticality, "tags": s.tags,
        "app_ids": s.app_ids,
        "network": {"ips": list(s.ips or []), "subnet": s.subnet,
                    "vlan": s.vlan, "datacenter": s.datacenter,
                    "cluster": s.cluster},
        "os_eol_bucket": s.os_eol_bucket or "",
        "warranty_bucket": s.warranty_bucket or "",
        "hardware_eol": s.hardware_eol or "",
        "utilization": s.utilization.to_dict(),
    }


def _code_profile_brief(p: Optional["CodeProfile"]) -> Optional[Dict[str, Any]]:
    """Compact code-profile snippet shared by the drawer's LLM actions so they
    see the app's runtime / framework / blockers / executor pattern, not just
    the host. None → omitted entirely (no fake placeholder)."""
    if p is None:
        return None
    return {
        "language": p.language, "runtime": p.runtime, "framework": p.framework,
        "cloud_readiness": p.cloud_readiness,
        "executor_pattern": p.migration_pattern,
        "blockers": list(p.blockers or []),
        "summary": p.summary,
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


# ---------------------------------------------------------------------------
# F8 (Phase B) — LLM-driven Landing Zone design interview
# ---------------------------------------------------------------------------
# The LLM proposes a parameterized ARCHETYPE BLUEPRINT (same shape as one
# LZ_BLUEPRINTS[arch] entry — vpc / peering / internet_egress / security_groups
# / cam_roles / tag_policy / policy_as_code), NEVER Terraform. The deterministic
# ``validate_lz_design`` is the authority: the LLM is looped until its proposal
# passes (CIDR non-overlap + symmetric peering + required keys), the same
# propose→validate→loop shape as the wave planner's cap revision and the 7R
# confidence clamp. The executor's ``iac-emit`` still emits HCL from the
# blueprint — this keeps idc-migrate the manager, the LLM the proposer, and the
# deterministic engine the gate.
LZ_DESIGN_SYSTEM = """You are a Tencent Cloud landing-zone architect. The operator describes their
migration landing zone in natural language; you design it as a parameterized
ARCHETYPE BLUEPRINT (the same shape the deterministic engine validates and the
executor turns into Terraform). You NEVER emit Terraform/HCL — only the blueprint.

A landing zone is 2-6 archetypes (the scale tier sets the count), each a VPC +
peering + egress + SG + CAM + tag policy + policy-as-code. Common patterns:
  hub-and-spoke : one hub (DC peering, intranet, NAT) + N spokes; each spoke
                  reaches the DC THROUGH the hub (no direct spoke->DC edge);
                  regulated workloads live in an isolated spoke (no peering).
  corp/online/dmz : hub (corp, DC link) + internet-exposed spoke (online, public
                  CLB+CDN+WAF) + isolated regulated tier (dmz, no peering).
  flat (small)   : 2 VPCs (online + corp), simple peering, no isolated tier.

Hard rules (the deterministic validator rejects violations and you WILL be
looped until your design passes):
- Every archetype's vpc.cidr must parse as a.b.c.d/len, NOT overlap another
  archetype's CIDR, and NOT overlap any on-prem CIDR given. Pick distinct /16s
  from a private range the on-prem estate does NOT use: if on-prem CIDRs are in
  10.x, carve cloud VPCs from 172.16.0.0/12 (e.g. 172.16.0.0/16, 172.17.0.0/16,
  …); if on-prem is in 172.16/12, use 10.x (e.g. 10.10.0.0/16, 10.20.0.0/16).
  Never reuse the on-prem ranges — overlapping CIDRs break hybrid DC peering.
- Plan IP space for ~10x growth: every workload VPC CIDR should be /24 or LARGER
  (a /16 is the sane default for a workload account). Re-addressing a live VPC
  is one of the hardest tasks in cloud engineering — do not under-size CIDRs.
- Peering is BIDIRECTIONAL: if archetype A declares "to_<B>": true then B MUST
  declare "to_<A>": true. Peering keys are "to_<other_archetype_name>" (omit the
  self-diagonal). A one-way edge is invalid and produces broken Terraform.
- Each archetype MUST carry ALL of: archetype, summary, vpc{name,cidr,description},
  peering{to_<...>}, internet_egress{...}, security_groups[], cam_roles[],
  tag_policy{}, policy_as_code[] (non-empty list of guardrule strings).
- The classifier (requirements.classifier) maps estate servers to YOUR archetype
  names. Precedence is tag_map (explicit operator tag, wins) -> app_map (which
  app the server hosts) -> role_map -> PENDING. It MUST reference only archetype
  names you defined. Cover the estate's real roles (e.g. web/frontend ->
  internet-exposed tier, db -> hub, regulated tags -> isolated tier). A server
  NO rule matches is NOT placed — it is left "pending" for the operator to
  classify (it never lands in a catch-all account, and never in the hub). So
  enumerate EVERY role and app the estate actually has in role_map / app_map so
  nothing falls to pending unintentionally; only genuinely unknown hosts stay
  pending.
- MULTI-WORKLOAD-ACCOUNT design: each archetype IS a workload account (one VPC +
  one account per archetype). Use ``app_map`` {app_id -> archetype} to assign
  specific apps to specific accounts — the estate summary lists the apps by id
  with their server count + dominant role + envs. Group apps by workload shape:
  Oracle/DB apps -> a data account; web/app apps -> an online/app account;
  Hadoop/Spark -> a big-data account; minio/gluster -> a storage account; etc.
  When the operator's prompt names apps, put exactly those app_ids in app_map.
  role_map is the fallback for apps you did not explicitly assign.
- Ground the design in the estate summary you are given (roles, envs, apps,
  criticality, regulation/exposure hints). Do not invent workloads not present.

The operator's demand (the AI prompt) customizes the USER-MODIFIABLE parts:
which apps land in which account (app_map), CIDRs, archetype names, extra
compliance tiers, tag policies. The SCALE TIER sets the DEFAULT STRATEGY for
everything else (account/networking/security/audit/budgeting + topology shape +
archetype count). Honor the scale's defaults unless the operator's demand
explicitly overrides one. The scale tier + its default matrix are in the estate
summary; you MAY add a requirements.governance block to override a default, but
the engine fills any you omit, so only emit governance when you are changing it.

Output ONLY a JSON object, no prose, no markdown fences, exactly this shape:
  {"name": "<short design name>",
   "summary": "<one-line description of this landing zone design — the account/VPC
               topology + the workload placement intent, so an operator can tell
               saved designs apart at a glance>",
   "onprem_cidrs": ["<cidr>", ...],
   "requirements": {"notes": "<1-2 sentences>",
                     "classifier": {"tag_map": {"<tag>":"<arch>"},
                                     "app_map": {"<app_id>":"<arch>"},
                                     "role_map": {"<role>":"<arch>"}},
   "archetypes": {
     "<arch_name>": {
       "archetype": "<arch_name>",
       "summary": "<one line>",
       "vpc": {"name": "vpc-<name>", "cidr": "<cidr>", "description": "<...>"},
       "peering": {"to_<other>": true|false, ...},
       "internet_egress": {"nat": <bool>, "public_clb": <bool>, "cdn": <bool>, "waf": <bool>},
       "security_groups": ["sg-..."],
       "cam_roles": ["role-..."],
       "tag_policy": {"env": "required", ...},
       "policy_as_code": ["<guardrail rule>", ...]}, ...}}"""


def _lz_design_system(scale: str) -> str:
    """The LZ architect system prompt, extended with the active scale tier's
    default strategy + the cross-scale golden rules. The scale block tells the
    LLM which parts are defaults (honor unless overridden) vs user-modifiable
    (the operator's demand). Falls back to ``medium`` for an unknown tier."""
    from ..core.lz import LZ_SCALE_TIERS, LZ_GOLDEN_RULES, LZ_MEDIUM
    tier = LZ_SCALE_TIERS.get(scale) or LZ_SCALE_TIERS[LZ_MEDIUM]
    lo, hi = tier["archetype_count"]
    if tier["topology"] == "flat":
        topo = ("Flat 2-VPC layout — NO isolated dmz tier, NO hub transit "
                "(online + corp only).")
    else:
        topo = ("Hub-and-spoke: a corp hub (DC peering, spokes reach DC via "
                "the hub) + an online spoke (public) + an isolated dmz tier "
                "for regulated workloads.")
    extras = ("Large tier extras: add Cloud Firewall (CFW) inspection + "
              "SCP-style guardrails to each archetype policy_as_code."
              if tier["cfw"] else "")
    rules = "\n".join(f"- {r['key']}: {r['rule']}" for r in LZ_GOLDEN_RULES)
    scale_block = (
        f"\n\nSCALE TIER: {tier['label']} — {tier['philosophy']}.\n"
        f"Target: {tier['target']}.\n"
        f"DEFAULT STRATEGY (honor these unless the operator's demand explicitly "
        f"overrides one):\n"
        f"- account_strategy: {tier['account_strategy']}\n"
        f"- networking: {tier['networking']}\n"
        f"- security: {tier['security']}\n"
        f"- audit: {tier['audit']}\n"
        f"- budgeting: {tier['budgeting']}\n"
        f"- identity: {tier['identity_rule']}\n"
        f"- topology: {tier['topology']}; produce {lo}-{hi} archetypes (each a "
        f"workload account). {topo}\n"
        f"{extras}\n"
        f"GOLDEN RULES (all scales — never violate):\n{rules}\n\n"
        f"USER-MODIFIABLE (set from the operator's demand, NOT the scale): "
        f"app_map (which apps in which account), CIDRs, archetype names, extra "
        f"compliance tiers, tag policies. Keep the default strategy for the rest.")
    return LZ_DESIGN_SYSTEM + scale_block


# regulation/exposure tag hints for the estate-context builder (kept local so
# the LLM helper doesn't couple to lz.py's private _DMZ_HINTS).
_LZ_REG_HINTS = {"idc:dmz", "regulated", "pci", "pci-dss", "pcidss", "hipaa",
                 "sox", "sensitive", "isolated", "compliance", "gdpr"}
_LZ_EXP_HINTS = {"idc:online", "public", "internet", "web", "frontend",
                 "exposed", "edge"}


def _lz_estate_context(servers: List[Server], matches: Optional[List[Match]] = None,
                       onprem_cidrs: Optional[List[str]] = None,
                       scale: Optional[str] = None) -> str:
    """Compact estate summary for the LZ architect (Phase B) — distributions by
    role/env/criticality/os + a per-app rollup (so the LLM can populate app_map
    and assign apps to workload accounts) + regulation + exposure tag hints +
    on-prem subnet hints (so the LLM picks cloud CIDRs that don't overlap
    on-prem) + any operator-supplied on-prem CIDRs. Grounds the proposal in the
    real estate so the LLM can't invent a hub/spoke for 5 web servers and call
    it done."""
    def dist(key):
        d: Dict[str, int] = {}
        for s in servers:
            k = key(s)
            if k:
                d[k] = d.get(k, 0) + 1
        return dict(sorted(d.items(), key=lambda x: -x[1]))

    reg: set = set()
    exp: set = set()
    for s in servers:
        for t in (s.tags or []):
            tl = t.lower().strip()
            # EXACT membership only — the old ``h in tl`` substring check flagged
            # "specification" as PCI (specifi[pci]ation) and similar false positives.
            if tl in _LZ_REG_HINTS:
                reg.add(tl)
            if tl in _LZ_EXP_HINTS:
                exp.add(tl)
    # per-app rollup: app_id -> {servers, role (dominant), envs, top_tag} so the
    # LLM can group apps into workload accounts (data / online / bigdata / ...).
    # Capped at the top 120 apps by server count to bound the prompt; the biggest
    # apps dominate the account shape and a real estate has hundreds of small ones.
    acc: Dict[str, Dict[str, Any]] = {}
    for s in servers:
        for aid in (s.app_ids or []):
            a = acc.setdefault(aid, {"servers": 0, "roles": {}, "envs": {}, "tags": {}})
            a["servers"] += 1
            if s.role:
                a["roles"][s.role] = a["roles"].get(s.role, 0) + 1
            if s.env:
                a["envs"][s.env] = a["envs"].get(s.env, 0) + 1
            for t in (s.tags or []):
                a["tags"][t] = a["tags"].get(t, 0) + 1
    by_app = []
    for aid, a in sorted(acc.items(), key=lambda kv: -kv[1]["servers"])[:120]:
        role = max(a["roles"], key=a["roles"].get) if a["roles"] else ""
        top_tag = max(a["tags"], key=a["tags"].get) if a["tags"] else ""
        env = max(a["envs"], key=a["envs"].get) if a["envs"] else ""
        by_app.append({"app_id": aid, "servers": a["servers"],
                       "role": role, "env": env, "top_tag": top_tag})
    subnets = sorted({s.subnet for s in servers if s.subnet})
    # the inferred scale tier (small/medium/large) drives the default strategy
    # the system prompt tells the LLM to honor; surface it + the estate-size
    # signals (servers / cores / memory / apps) that picked it so the LLM sizes
    # the LZ to the real hardware, not just host count.
    from ..core.lz import scale_for, estate_scale_metrics, LZ_SCALE_TIERS
    m = estate_scale_metrics(servers)
    tier = scale_for(servers, scale_override=scale, metrics=m)
    payload = {
        "total_servers": m["servers"],
        "total_apps": len(acc),
        "total_cores": m["cores"],
        "total_memory_gb": m["memory_gb"],
        "baremetal_hosts": m["baremetal"],
        "scale_tier": tier,
        "scale_label": LZ_SCALE_TIERS[tier]["label"],
        "by_role": dist(lambda s: s.role),
        "by_env": dist(lambda s: s.env),
        "by_criticality": dist(lambda s: s.business_criticality),
        "by_os": dist(lambda s: s.os),
        "by_app": by_app,
        "regulation_tag_hints": sorted(reg),
        "exposure_tag_hints": sorted(exp),
        "onprem_subnet_hints": subnets[:40],
        "operator_supplied_onprem_cidrs": list(onprem_cidrs or []),
    }
    return ("Design a Tencent Cloud landing zone for this estate at the "
            f"{LZ_SCALE_TIERS[tier]['label']} scale (the default strategy for "
            "this tier is in the system prompt — honor it; the operator's "
            "demand below customizes the user-modifiable parts). Size the LZ to "
            f"the real estate: {m['servers']} servers, {m['cores']:,} vCPU cores, "
            f"{m['memory_gb']:,} GB memory, {m['baremetal']} bare-metal hosts. "
            "by_app lists the apps you can assign to workload accounts via "
            "requirements.classifier.app_map. Estate summary (JSON):\n"
            + json.dumps(payload, ensure_ascii=False, indent=2))