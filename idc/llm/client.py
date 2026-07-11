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
from ..core.models import Match, Server, Wave

UNAVAILABLE = "[LLM unavailable — rule-based result only]"


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
            return f"{fallback} (LLM error: {e!r})"

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
                          fallback="(could not answer — LLM unavailable)")

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

    def summarize_wave(self, wave: Wave, servers: List[Server],
                       matches: List[Match]) -> str:
        return self._safe(lambda: self._summarize_wave(wave, servers, matches),
                          fallback="(wave summary unavailable — LLM error)")

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

    def right_size(self, server: Server, match: Match) -> str:
        return self._safe(lambda: self._right_size(server, match),
                          fallback="(right-size unavailable — LLM error)")

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