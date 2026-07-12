"""F8 — Landing Zone parameterized blueprint + policy-as-code.

Modularizes the Landing Zone into archetypes (à la Azure ALZ Corp/Online/DMZ)
so the LZ Terraform the agent emits is parameterized by workload placement,
not one-shot. Each archetype maps to a Tencent Cloud VPC / peering / CAM /
security-group / tag-policy template the agent consumes as context.

Three archetypes:
  * ``corp``   — hybrid connectivity: DC VPN/direct-connect peering, intranet
                 only, no public ingress. Default for internal apps / DBs /
                 infra that need the DC link.
  * ``online`` — internet-exposed: public CLB + CDN + WAF in front, NAT egress.
                 For web/frontend/cutover-tier workloads.
  * ``dmz``    — isolated: no direct internet, tightly-controlled peering only.
                 For regulated / sensitive workloads (tag ``idc:dmz``).

``archetype_for`` is a deterministic classifier (tags > role > default).
``lz_readiness`` reads the per-archetype **LZ status** the operator persists
(``Store.get_lz_status``) — an archetype is ``ready`` only when its LZ Terraform
has been applied + finalized (the operator marks it; the agent emits the
Terraform from ``LZ_BLUEPRINTS``). Default is ``not_ready`` so a workload wave
is gated until its target archetype's LZ is explicitly stood up — the safe
default (à la Azure ``Deploy-VNET-HubSpoke`` auto-deploying spokes).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .models import Match, Server, _now

# ---------------------------------------------------------------------------
# archetypes — the parameterized LZ blueprint (Tencent Cloud mapping)
# ---------------------------------------------------------------------------
LZ_CORP = "corp"
LZ_ONLINE = "online"
LZ_DMZ = "dmz"
LZ_ARCHETYPES = (LZ_CORP, LZ_ONLINE, LZ_DMZ)

# LZ lifecycle status (persisted per archetype by the operator).
LZ_NOT_READY = "not_ready"     # default — LZ Terraform not applied yet
LZ_APPLIED = "applied"         # Terraform applied, not yet validated/finalized
LZ_FINALIZED = "finalized"     # LZ live + validated → workloads can land
LZ_STATUSES = (LZ_NOT_READY, LZ_APPLIED, LZ_FINALIZED)

# tag conventions an operator can set to force an archetype (à la Azure
# AzM.MigrationIntent). Highest precedence in archetype_for.
TAG_DMZ = "idc:dmz"
TAG_ONLINE = "idc:online"
TAG_CORP = "idc:corp"

# Tencent Cloud resource template per archetype. Consumed by the agent as
# context (claude_runner.lz_context) so it emits per-archetype Terraform. This
# is data, not code generation — the agent does the actual HCL emit.
LZ_BLUEPRINTS: Dict[str, Dict[str, Any]] = {
    LZ_CORP: {
        "archetype": LZ_CORP,
        "summary": "Hybrid connectivity LZ — DC peering + intranet only.",
        "vpc": {"name": "vpc-corp", "cidr": "10.10.0.0/16",
                "description": "corp VPC with DC direct-connect / VPN peering"},
        "peering": {"to_dc": True, "to_online": True, "to_dmz": False,
                    "description": "peers to DC + online; NOT to dmz (isolation)"},
        "internet_egress": {"nat": True, "public_clb": False},
        "security_groups": ["sg-corp-web-intra", "sg-corp-db-intra"],
        "cam_roles": ["corp-workload-role", "corp-dc-link-role"],
        "tag_policy": {"cost_center": "required", "env": "required",
                       "archetype": "corp"},
        "policy_as_code": [
            "deny public CLB in corp VPC (intranet only)",
            "require DC peering healthy before workload deploy",
            "require cost_center + env tags on every resource",
        ],
    },
    LZ_ONLINE: {
        "archetype": LZ_ONLINE,
        "summary": "Internet-exposed LZ — public CLB + CDN + WAF in front.",
        "vpc": {"name": "vpc-online", "cidr": "10.20.0.0/16",
                "description": "online VPC with public ingress"},
        "peering": {"to_dc": True, "to_online": False, "to_dmz": False,
                    "description": "peers to DC (for backend calls); not to dmz"},
        "internet_egress": {"nat": True, "public_clb": True, "cdn": True, "waf": True},
        "security_groups": ["sg-online-web-pub", "sg-online-app-intra"],
        "cam_roles": ["online-workload-role", "online-clb-manage-role"],
        "tag_policy": {"cost_center": "required", "env": "required",
                       "archetype": "online", "public_exposure": "required"},
        "policy_as_code": [
            "public CLB must sit behind WAF",
            "CDN origin must be online VPC only",
            "require public_exposure tag on every public resource",
        ],
    },
    LZ_DMZ: {
        "archetype": LZ_DMZ,
        "summary": "Isolated LZ — no direct internet, tightly-controlled peering.",
        "vpc": {"name": "vpc-dmz", "cidr": "10.30.0.0/16",
                "description": "dmz VPC — isolated, regulated workloads"},
        "peering": {"to_dc": False, "to_online": False, "to_dmz": False,
                    "description": "no peering — fully isolated (regulated)"},
        "internet_egress": {"nat": False, "public_clb": False,
                            "description": "no internet path in or out"},
        "security_groups": ["sg-dmz-locked-down"],
        "cam_roles": ["dmz-workload-role"],
        "tag_policy": {"cost_center": "required", "env": "required",
                       "archetype": "dmz", "regulated": "required"},
        "policy_as_code": [
            "deny all internet ingress + egress",
            "deny peering to corp / online (isolation)",
            "require regulated tag + compliance approval before deploy",
        ],
    },
}


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------
_DMZ_HINTS = {"idc:dmz", "regulated", "pci", "hipaa", "sox", "sensitive", "isolated"}
_ONLINE_ROLES = {"web", "frontend"}


def archetype_for(server: Server, match: Optional[Match] = None) -> str:
    """Classify a server into an LZ archetype (F8, deterministic).

    Precedence (highest first):
      1. explicit tag (``idc:dmz`` / ``idc:online`` / ``idc:corp``) — operator
         override, à la Azure ``AzM.MigrationIntent``;
      2. regulated/sensitive hints in tags → ``dmz``;
      3. web/frontend role → ``online`` (internet-exposed);
      4. default → ``corp`` (hybrid connectivity).

    The ``Workload.tier`` ("frontend"/"web") is not available on the Server,
    so the role carries that signal (a frontend host has ``role=web``). Pure:
    same inputs -> same archetype, no DB, no LLM.
    """
    tags = {t.lower().strip() for t in (server.tags or [])}
    if TAG_DMZ in tags:
        return LZ_DMZ
    if TAG_ONLINE in tags:
        return LZ_ONLINE
    if TAG_CORP in tags:
        return LZ_CORP
    if tags & _DMZ_HINTS:
        return LZ_DMZ
    role = (server.role or "").lower()
    if role in _ONLINE_ROLES:
        return LZ_ONLINE
    return LZ_CORP


def archetypes_for_servers(servers: Sequence[Server],
                           matches: Optional[Sequence[Match]] = None
                           ) -> Dict[str, str]:
    """server_id -> archetype for a batch. The match is optional (archetype_for
    currently keys off server signals only, but the signature reserves the
    match for future target-region-aware placement)."""
    m_by_id = {m.server_id: m for m in (matches or [])}
    return {s.id: archetype_for(s, m_by_id.get(s.id)) for s in servers}


# ---------------------------------------------------------------------------
# readiness — is an archetype's LZ finalized? (placement gate)
# ---------------------------------------------------------------------------
def lz_readiness(store, servers: Sequence[Server],
                 matches: Optional[Sequence[Match]] = None
                 ) -> Dict[str, Dict[str, Any]]:
    """Per-archetype LZ readiness, read from the persisted LZ status (F8).

    An archetype is ``ready`` only when its LZ Terraform has been applied +
    finalized (operator marks it via ``Store.set_lz_status`` after the agent
    emits + the operator applies the archetype's blueprint). Default is
    ``not_ready`` so workload waves are gated until the target LZ is explicitly
    stood up — the safe default.

    Returns ``{archetype: {status, ready, workload_count}}`` where
    ``workload_count`` is how many estate servers target that archetype (for
    the UI / F10 readiness heatmap).
    """
    arch_of = archetypes_for_servers(list(servers), matches)
    counts = {a: 0 for a in LZ_ARCHETYPES}
    for a in arch_of.values():
        counts[a] = counts.get(a, 0) + 1
    statuses = store.list_lz_status()
    out: Dict[str, Dict[str, Any]] = {}
    for arch in LZ_ARCHETYPES:
        st = statuses.get(arch, LZ_NOT_READY)
        out[arch] = {"status": st, "ready": st == LZ_FINALIZED,
                     "workload_count": counts.get(arch, 0)}
    return out


def wave_archetypes(wave_server_ids: Sequence[str], servers: Sequence[Server],
                    matches: Optional[Sequence[Match]] = None
                    ) -> Dict[str, int]:
    """The archetype mix of a wave's members (for the UI + the placement gate)."""
    srv_by_id = {s.id: s for s in servers}
    arch_of = archetypes_for_servers(list(srv_by_id.values()), matches)
    mix: Dict[str, int] = {a: 0 for a in LZ_ARCHETYPES}
    for sid in wave_server_ids:
        arch = arch_of.get(sid, LZ_CORP)
        mix[arch] = mix.get(arch, 0) + 1
    return mix


def check_lz_gate(store, wave_id: str, servers: Sequence[Server],
                  matches: Optional[Sequence[Match]] = None
                  ) -> Dict[str, Any]:
    """Placement gate (F8): can this wave launch, given LZ archetype readiness?

    Returns ``{ok, blocking_archetypes[]}`` — ``ok=False`` when any archetype
    the wave needs has an LZ that isn't finalized. The LZ wave itself always
    passes (it IS the LZ). Feeds F6 ``launch_wave`` as a pre-check + F10
    readiness.
    """
    from .models import STAGE_LZ
    wave = None
    for w in store.list_waves():
        if w.id == wave_id:
            wave = w
            break
    if wave is None:
        return {"ok": False, "blocking_archetypes": [],
                "reason": f"wave {wave_id} not found"}
    if wave.stage == STAGE_LZ:
        return {"ok": True, "blocking_archetypes": [],
                "reason": "LZ wave — not gated on LZ readiness"}
    mix = wave_archetypes(wave.server_ids or [], servers, matches)
    readiness = lz_readiness(store, servers, matches)
    blocking = [a for a in LZ_ARCHETYPES
                if mix.get(a, 0) > 0 and not readiness[a]["ready"]]
    return {"ok": not blocking, "blocking_archetypes": blocking,
            "archetype_mix": mix, "reason": (
                "all target archetypes ready" if not blocking
                else f"{len(blocking)} archetype(s) not ready: "
                     + ", ".join(blocking))}


__all__ = [
    "LZ_CORP", "LZ_ONLINE", "LZ_DMZ", "LZ_ARCHETYPES",
    "LZ_NOT_READY", "LZ_APPLIED", "LZ_FINALIZED", "LZ_STATUSES",
    "TAG_DMZ", "TAG_ONLINE", "TAG_CORP",
    "LZ_BLUEPRINTS",
    "archetype_for", "archetypes_for_servers",
    "lz_readiness", "wave_archetypes", "check_lz_gate",
]