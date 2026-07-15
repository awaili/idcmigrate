"""LLM wave-policy proposer.

Turns a natural-language migration demand into a structured ``WavePolicy``
that the deterministic engine (``idc.core.llm_plan``) instantiates over the
estate. The LLM only emits a small policy (a few stages), never 15K waves —
that's what makes this scalable and auditable.

Flow:  estate_summary()  ->  LLM (schema-constrained prompt)  ->  JSON parse
       ->  WavePolicy.from_dict  ->  schema validate. Falls back with a clear
       error if the model output is unparseable.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config import Settings, get_settings
from ..core.llm_plan import WavePolicy
from ..core.models import AppStrategy, CodeProfile, Match, Server, Workload
from .client import LLMClient, UNAVAILABLE

SYSTEM = """You are a cloud-migration wave planner for Tencent Cloud. Given an
estate summary and the operator's demand, design a WAVE POLICY as JSON.

A policy is: {"notes": "...", "stages": [ <WaveSpec>, ... ]}.
Stages run in array order. Every server is assigned to the FIRST stage whose
filters match it; unmatched servers go to a catch-all wave.

WaveSpec fields (all filters are lists = OR-within, AND-across; omit a filter
to mean "no constraint"):
  label            string, unique, short (e.g. "LZ", "Data", "Apps", "Cutover")
  stage            one of: "1_landing_zone", "2_data", "3_application", "4_cutover"
  rationale        short string
  roles            server roles to include (e.g. ["db","cache"])
  envs             e.g. ["prod"]
  os               e.g. ["centos","ubuntu"]
  target_products  Tencent products (e.g. ["CDB","TencentDB for Redis"])
  criticalities    e.g. ["high"]
  clusters         e.g. ["cl-db"]
  regions          e.g. ["ap-bangkok"]
  tags             server must have ANY of these tags
  app_ids          include servers belonging to these apps
  group_by         "none" | "app" | "cluster" | "env" | "region" | "role" | "target"
  max_per_wave     int, 0 = no cap (use ~50-100 to keep waves manageable)
  depends_on       list of OTHER stage labels that must finish first
  use_app_deps     bool (default true; with group_by=app, order apps by their
                   dependency DAG and set inter-wave deps)

Rules:
- DEFAULT to BATCHING apps, not one-wave-per-app. For application stages use
  group_by="none" with max_per_wave ~50-100 servers (or 0 = one big wave) so
  a wave holds MANY apps. One-wave-per-app (group_by="app") explodes the wave
  count (N apps -> N waves) — only use it when the operator EXPLICITLY asks
  for per-app ordering / "每个app一个wave" / "migrate app by app".
- group_by="app" produces ONE wave per app (N apps -> N waves); each stage's
  waves add up. To BOUND the total wave count (the operator may give a max),
  prefer group_by="none" with a large max_per_wave, batch apps into fewer
  stages, or drop max_per_wave. A hard cap may be enforced after instantiation.
- When you DO use group_by="app", set use_app_deps=true so app dependencies
  drive wave order.
- Put infrastructure (k8s/monitoring) in 1_landing_zone, data (db/cache) in
  2_data, apps in 3_application, public frontends in 4_cutover — UNLESS the
  operator's demand says otherwise. Follow the operator's demand over these
  defaults.
- Keep stages to ~4-8. Each stage's filters should partition the estate.
- Respect the operator's priorities, grouping, sizing, and sequencing.
- If the operator specifies a maximum number of waves, treat it as a HARD
  cap on the total instantiated wave count, not the stage count.

Output ONLY the JSON object (no prose, no markdown fences)."""


def estate_summary(servers: List[Server], workloads: List[Workload],
                   matches: Optional[List[Match]] = None,
                   scope_ids: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Compact estate description for the LLM (distributions + app deps)."""
    m_by_id = {m.server_id: m for m in (matches or [])}
    if scope_ids is not None:
        servers = [s for s in servers if s.id in scope_ids]
    n = len(servers)

    def dist(key, default=""):
        d: Dict[str, int] = {}
        for s in servers:
            k = key(s)
            if k:
                d[k] = d.get(k, 0) + 1
        return dict(sorted(d.items(), key=lambda x: -x[1]))

    by_role = dist(lambda s: s.role)
    by_env = dist(lambda s: s.env)
    by_os = dist(lambda s: s.os)
    by_crit = dist(lambda s: s.business_criticality)
    by_target = dist(lambda s: (m_by_id.get(s.id).target.product if m_by_id.get(s.id) else None) or "unmatched")
    by_region = dist(lambda s: (m_by_id.get(s.id).target.region if m_by_id.get(s.id) else None) or "unmatched")
    cores = sum(s.cpu_cores for s in servers)
    high_util = sum(1 for s in servers
                    if (s.utilization.mem_p95 or 0) >= 80
                    or (s.utilization.disk_used_pct or 0) >= 80)

    # app dependency highlights — cap hard to keep the prompt small (latency).
    # Show the biggest apps + any with dependencies (those drive wave order).
    apps_all = [w for w in workloads
                if scope_ids is None or any(sid in scope_ids for sid in w.server_ids)]
    apps_all.sort(key=lambda w: len(w.server_ids), reverse=True)
    apps = [{"app_id": w.app_id, "tier": w.tier, "servers": len(w.server_ids),
             "depends_on": w.depends_on} for w in apps_all[:30]]

    return {
        "total_servers": n, "total_cores": cores, "high_utilization": high_util,
        "by_role": by_role, "by_env": by_env, "by_os": by_os,
        "by_criticality": by_crit,
        "by_target_product": by_target, "by_region": by_region,
        "apps_sample": apps, "app_count": len(apps_all),
        "scope": "scoped subset" if scope_ids is not None else "whole estate",
    }


# max revision rounds after the first proposal (each is one LLM round-trip).
MAX_REVISIONS = 2

# operators phrase wave caps many ways; "最高分10个wave" is a real typo we
# tolerate by allowing a little non-digit noise between the cap word and the
# number. Explicit max_waves arg always wins over this.
_CAP_RE = re.compile(
    r"(?:最多|最高|不超过|至多|上限|最多不超过|max(?:imum)?|at most|no more than|≤)"
    r"[^0-9]{0,6}(\d+)\s*个?\s*waves?",
    re.IGNORECASE,
)


def _parse_max_waves(demand: str) -> Optional[int]:
    """Best-effort parse of a wave-count cap from the natural-language demand."""
    if not demand:
        return None
    m = _CAP_RE.search(demand)
    if m:
        try:
            n = int(m.group(1))
            return n if n > 0 else None
        except ValueError:
            return None
    return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text or text == UNAVAILABLE:
        return None
    text = text.strip()
    # 1) the common case: the whole reply (or the fenced block) is the object.
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidates = [text, fenced.group(1)] if fenced else [text]
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
    # 2) brace counting that skips string literals so a "}" inside a string
    #    value doesn't prematurely close the object.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
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
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class Planner:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.llm = LLMClient(self.settings)

    def propose(self, demand: str, servers: List[Server],
                workloads: List[Workload], matches: Optional[List[Match]] = None,
                scope_ids: Optional[Set[str]] = None,
                max_waves: Optional[int] = None,
                profiles: Optional[List["CodeProfile"]] = None,
                strategies: Optional[Dict[str, "AppStrategy"]] = None) -> Dict[str, Any]:
        """Return the proposed plan as a dict.

        Keys: {policy, waves, raw, errors, warnings, summary, waves_count,
        revisions, max_waves, engine_capped}. ``policy`` is None if the LLM
        output was unparseable; ``waves`` is the authoritative final wave list
        (already capped to ``max_waves`` and with retain/retire apps routed to
        trailing disposition waves) — callers persist THESE waves, they do NOT
        re-instantiate the policy themselves.

        When ``max_waves`` is set (explicitly or parsed from the demand), the
        policy is revised up to MAX_REVISIONS times until the engine
        instantiates it into <= max_waves waves; ``cap_waves`` is the
        deterministic backstop that guarantees the cap. ``revisions`` records
        each round-trip for the UI; ``engine_capped`` flags when the backstop
        (not the LLM) did the merging.

        ``profiles`` — 7R retain/retire apps (per
        ``CodeProfile.migration_pattern``) are excluded from the migration
        waves and routed to trailing Retain/Retire disposition waves (shared
        servers with a migrating app still migrate).

        ``strategies`` — optional ``{app_id: AppStrategy}`` overlay (AI-assigned
        7R). retain/retire are read from this overlay first, then profiles.
        Lets a 7R result drive exclusion WITHOUT first persisting to DB.
        """
        # function-level import to avoid a core <-> llm import cycle
        from ..core.llm_plan import build_from_policy

        cap = max_waves if max_waves and max_waves > 0 else _parse_max_waves(demand)
        summary = estate_summary(servers, workloads, matches, scope_ids)
        user = (
            f"Operator demand:\n{demand}\n\n"
            f"Estate summary (JSON):\n{json.dumps(summary, ensure_ascii=False)}\n\n"
            "Produce the wave policy JSON now."
        )
        try:
            raw = self.llm.chat([
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ])
        except Exception as e:
            # chat() raises on gateway/network error (raise_for_status) — degrade
            # gracefully to the documented error shape instead of a raw 500.
            return {"policy": None, "waves": None, "raw": "",
                    "errors": [f"MigraQ error: {e!r}"], "warnings": [],
                    "summary": summary, "waves_count": None,
                    "revisions": [], "max_waves": cap, "engine_capped": False}
        obj = _extract_json(raw)
        if obj is None:
            return {"policy": None, "waves": None, "raw": raw,
                    "errors": ["MigraQ output was not valid JSON; no policy produced."],
                    "warnings": [], "summary": summary, "waves_count": None,
                    "revisions": [], "max_waves": cap, "engine_capped": False}
        policy = WavePolicy.from_dict(obj)
        schema_errs = policy.validate_schema()
        revisions: List[Dict[str, Any]] = []

        # schema-invalid -> can't instantiate; bail with the policy for display
        if schema_errs:
            return {"policy": policy, "waves": None, "raw": raw, "errors": schema_errs,
                    "warnings": [], "summary": summary, "waves_count": None,
                    "revisions": revisions, "max_waves": cap, "engine_capped": False}

        # --- optional revision loop: keep the LLM honest about the cap -------
        # Only runs when a cap is set. Produces best_policy + uncapped count
        # (the count the LLM's policy yields before cap_waves force-merges).
        best_policy = policy
        uncapped: Optional[int] = None
        if cap:
            best_policy, best_count, revisions = self._revision_loop(
                policy, servers, workloads, matches, scope_ids, cap, summary, demand,
                profiles, strategies)
            uncapped = best_count

        # --- ONE authoritative instantiation (capped + retain/retire-excluded) -
        # This is the single source of truth callers persist; no re-instantiation
        # downstream, so the cap + 7R exclusion can't be forgotten/diverge.
        final_rep = build_from_policy(best_policy, servers, workloads, matches, scope_ids,
                                      max_waves=cap or 0, profiles=profiles or [],
                                      strategies=strategies)
        engine_notes: List[str] = []
        if cap and uncapped is not None and uncapped > cap \
                and len(final_rep.waves) <= cap:
            engine_notes.append(
                f"engine force-merged {uncapped} -> {len(final_rep.waves)} waves "
                f"to honor cap {cap}.")
        # engine-only errors not already captured by schema_errs (e.g. a stage
        # with no label was skipped) are real errors; the force-merge note is NOT
        # an error (the plan is valid) — it goes to warnings, with engine_capped
        # as the structured signal.
        extra_errs = [e for e in final_rep.errors if e not in schema_errs]
        return {
            "policy": best_policy, "waves": final_rep.waves, "raw": raw,
            "errors": schema_errs + extra_errs,
            "warnings": list(final_rep.warnings) + engine_notes,
            "summary": summary, "waves_count": len(final_rep.waves),
            "revisions": revisions, "max_waves": cap,
            "engine_capped": bool(engine_notes),
        }

    def _revision_loop(self, policy: WavePolicy, servers: List[Server],
                       workloads: List[Workload], matches: Optional[List[Match]],
                       scope_ids: Optional[Set[str]], cap: int,
                       summary: Dict[str, Any], demand: str,
                       profiles: Optional[List["CodeProfile"]],
                       strategies: Optional[Dict[str, "AppStrategy"]]
                       ) -> Tuple[WavePolicy, Optional[int], List[Dict[str, Any]]]:
        """Re-prompt the LLM up to MAX_REVISIONS times until the engine yields
        <= cap waves. Returns (best_policy, uncapped_wave_count, revisions)."""
        best_policy, best_count = policy, None
        cur = policy
        revisions: List[Dict[str, Any]] = []
        for attempt in range(1, MAX_REVISIONS + 1):
            count = self._waves_count(cur, servers, workloads, matches, scope_ids,
                                       profiles, strategies)
            if best_count is None or (count is not None and count < best_count):
                best_policy, best_count = cur, count
            if count is not None and count <= cap:
                break  # within cap — done
            revised, rev_raw, rev_err = self._revise(
                cur, servers, workloads, matches, scope_ids, cap, count, summary, demand,
                profiles, strategies)
            revisions.append({
                "attempt": attempt, "waves_before": count, "waves_after": None,
                "raw": (rev_raw or "")[:800], "errors": rev_err, "accepted": False,
            })
            if revised is None:
                break  # parse/schema failure or LLM unavailable — give up revising
            revisions[-1]["waves_after"] = self._waves_count(
                revised, servers, workloads, matches, scope_ids, profiles, strategies)
            revisions[-1]["accepted"] = revisions[-1]["waves_after"] is not None \
                and revisions[-1]["waves_after"] <= cap
            cur = revised
            if revisions[-1]["accepted"]:
                best_policy, best_count = revised, revisions[-1]["waves_after"]
                break
        return best_policy, best_count, revisions

    # -- revision helpers ------------------------------------------------
    def _waves_count(self, policy: WavePolicy, servers: List[Server],
                     workloads: List[Workload], matches: Optional[List[Match]],
                     scope_ids: Optional[Set[str]],
                     profiles: Optional[List["CodeProfile"]] = None,
                     strategies: Optional[Dict[str, "AppStrategy"]] = None) -> Optional[int]:
        """Instantiate the policy and return the resulting wave count, or
        None if the schema is invalid (can't instantiate cleanly). Non-fatal
        engine warnings (e.g. a skipped unlabeled stage) do NOT block — we
        only care about the wave count."""
        if policy.validate_schema():
            return None
        from ..core.llm_plan import build_from_policy
        try:
            rep = build_from_policy(policy, servers, workloads, matches, scope_ids,
                                    profiles=profiles or [], strategies=strategies)
        except Exception:
            return None
        return len(rep.waves)

    def _revise(self, policy: WavePolicy, servers: List[Server],
                workloads: List[Workload], matches: Optional[List[Match]],
                scope_ids: Optional[Set[str]], cap: int, count: Optional[int],
                summary: Dict[str, Any], demand: str,
                profiles: Optional[List["CodeProfile"]] = None,
                strategies: Optional[Dict[str, "AppStrategy"]] = None) -> Tuple[Optional[WavePolicy], str, List[str]]:
        """Ask the LLM to revise the policy so the engine produces <= cap waves.
        Returns (revised_policy | None, raw, errors). None means give up."""
        from ..core.llm_plan import build_from_policy
        rep = build_from_policy(policy, servers, workloads, matches, scope_ids,
                                profiles=profiles or [], strategies=strategies)
        wave_brief = "\n".join(
            f"- {i}: {w.name} · {w.stage} · {len(w.server_ids)} servers"
            for i, w in enumerate(rep.waves[:80]))
        if len(rep.waves) > 80:
            wave_brief += f"\n- …and {len(rep.waves) - 80} more"
        cur_json = json.dumps(
            {"notes": policy.notes,
             "stages": [{k: v for k, v in vars(s).items()} for s in policy.stages]},
            ensure_ascii=False, indent=2)
        user = (
            f"Operator demand:\n{demand}\n\n"
            f"Estate summary (JSON):\n{json.dumps(summary, ensure_ascii=False)}\n\n"
            f"Your current policy (JSON):\n{cur_json}\n\n"
            f"The deterministic engine instantiated it into {len(rep.waves)} waves "
            f"(cap is {cap}). Produced waves (index, name, stage, server_count):\n"
            f"{wave_brief}\n\n"
            "Revise the policy so the engine produces AT MOST the cap number of "
            "waves, while preserving migration order (landing_zone -> data -> apps "
            "-> cutover) and NOT breaking app dependency order. Levers: fewer "
            "stages, group_by=\"none\", larger or zero max_per_wave, batch apps into "
            "one stage. Output ONLY the revised policy JSON (same schema)."
        )
        try:
            raw = self.llm.chat([
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ])
        except Exception as e:
            return None, "", [f"MigraQ error: {e!r}"]
        if not raw or raw == UNAVAILABLE:
            return None, raw or "", ["MigraQ unavailable; cannot revise policy."]
        obj = _extract_json(raw)
        if obj is None:
            return None, raw, ["revision output was not valid JSON; kept prior policy."]
        revised = WavePolicy.from_dict(obj)
        errs = revised.validate_schema()
        if errs:
            return None, raw, errs
        return revised, raw, []


def get_planner(settings: Optional[Settings] = None) -> Planner:
    return Planner(settings)