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
from typing import Any, Dict, List, Optional, Set

from ..config import Settings, get_settings
from ..core.llm_plan import WavePolicy
from ..core.models import Match, Server, Workload
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
  regions          e.g. ["ap-shanghai"]
  tags             server must have ANY of these tags
  app_ids          include servers belonging to these apps
  group_by         "none" | "app" | "cluster" | "env" | "region" | "role" | "target"
  max_per_wave     int, 0 = no cap (use ~50-100 to keep waves manageable)
  depends_on       list of OTHER stage labels that must finish first
  use_app_deps     bool (default true; with group_by=app, order apps by their
                   dependency DAG and set inter-wave deps)

Rules:
- Use group_by="app" with use_app_deps=true for application stages so app
  dependencies drive wave order.
- Put infrastructure (k8s/monitoring) in 1_landing_zone, data (db/cache) in
  2_data, apps in 3_application, public frontends in 4_cutover — UNLESS the
  operator's demand says otherwise. Follow the operator's demand over these
  defaults.
- Keep stages to ~4-8. Each stage's filters should partition the estate.
- Respect the operator's priorities, grouping, sizing, and sequencing.

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


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text or text == UNAVAILABLE:
        return None
    text = text.strip()
    # strip ```json ... ``` fences
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    # find the first balanced { ... }
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
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
                scope_ids: Optional[Set[str]] = None) -> Dict[str, Any]:
        """Return {policy, raw, errors}. policy is None if parsing failed."""
        summary = estate_summary(servers, workloads, matches, scope_ids)
        user = (
            f"Operator demand:\n{demand}\n\n"
            f"Estate summary (JSON):\n{json.dumps(summary, ensure_ascii=False)}\n\n"
            "Produce the wave policy JSON now."
        )
        raw = self.llm.chat([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ])
        obj = _extract_json(raw)
        if obj is None:
            return {"policy": None, "raw": raw,
                    "errors": ["LLM output was not valid JSON; no policy produced."]}
        policy = WavePolicy.from_dict(obj)
        schema_errs = policy.validate_schema()
        return {"policy": policy, "raw": raw, "errors": schema_errs,
                "summary": summary}


def get_planner(settings: Optional[Settings] = None) -> Planner:
    return Planner(settings)