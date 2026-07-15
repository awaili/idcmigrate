"""F8 — Landing Zone parameterized blueprint + policy-as-code.

Modularizes the Landing Zone into archetypes (à la Azure ALZ Corp/Online/DMZ)
so the LZ Terraform the agent emits is parameterized by workload placement,
not one-shot. Each archetype maps to a Tencent Cloud VPC / peering / CAM /
security-group / tag-policy template the agent consumes as context.

Three archetypes laid out as a **hub-and-spoke** (à la Azure ALZ
corp/online, Tencent CCN-centric):
  * ``corp``   — the hub: hybrid connectivity, DC VPN/direct-connect peering,
                 intranet only, no public ingress. Peers to the DC and to the
                 ``online`` spoke. Default for internal apps / DBs / infra that
                 need the DC link. Spokes reach the DC *through* corp, never
                 directly.
  * ``online`` — a spoke: internet-exposed (public CLB + CDN + WAF in front,
                 NAT egress). Peers to the ``corp`` hub only — reaches the DC
                 via corp transit, not a direct peering (the internet tier must
                 not have a route straight to on-prem). For web/frontend/
                 cutover-tier workloads.
  * ``dmz``    — isolated: no direct internet, no peering at all (regulated).
                 For regulated / sensitive workloads (tag ``idc:dmz``).

Peering is a **symmetric** matrix: if ``corp.to_online`` is True then
``online.to_corp`` must be True too — Tencent/Azure/AWS peering is
bidirectional, so a one-way declaration is invalid and the agent (which
consumes ``LZ_BLUEPRINTS`` verbatim as Terraform context) would emit broken
HCL. ``_assert_peering_symmetric`` enforces this invariant at import.

``archetype_for`` is a deterministic classifier (tags > role > corp, with
signal-less hosts left ``pending`` rather than dumped into the hub).
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

# ``pending`` is NOT an archetype — it has no VPC / account / blueprint. It is the
# marker for a host the classifier could NOT place (no explicit tag / app / role
# rule matched it). Such a host is left for the operator to classify manually
# rather than silently dumped into the hub (corp) account — the hub is for hybrid
# connectivity, not a catch-all bucket for workloads the design didn't account for.
# It flows through the placement gate as a blocker (a wave with pending members
# can't launch until every host is classified) and through the network designer as
# intentionally unplaced (no VPC, no IP).
LZ_PENDING = "pending"

# LZ lifecycle status (persisted per archetype by the operator).
LZ_NOT_READY = "not_ready"     # default — LZ Terraform not applied yet
LZ_APPLIED = "applied"         # Terraform applied, not yet validated/finalized
LZ_FINALIZED = "finalized"     # LZ live + validated → workloads can land
LZ_STATUSES = (LZ_NOT_READY, LZ_APPLIED, LZ_FINALIZED)

# ---------------------------------------------------------------------------
# scale tiers — the LZ scales with operational maturity + business complexity.
# The scale tier drives the DEFAULT STRATEGY (the small/medium/large comparison
# matrix: account / networking / security / audit / budgeting + the topology
# shape + archetype count). The operator-customizable parts (which apps land in
# which account, CIDRs, archetype names, extra compliance tiers) flow through
# the AI Prompt — the LLM interview (LLMClient.design_lz). Everything else is
# the scale's default, auto-filled so the operator does not have to author it.
# See ``LZ_GOLDEN_RULES`` for the cross-scale invariants the validator enforces
# regardless of tier.
# ---------------------------------------------------------------------------
LZ_SMALL = "small"     # Start-up / Dev  — speed & simplicity
LZ_MEDIUM = "medium"   # Growth / SME    — control & standardization
LZ_LARGE = "large"     # Enterprise      — governance & automated compliance
LZ_SCALE_TIERS_ORDER = (LZ_SMALL, LZ_MEDIUM, LZ_LARGE)

# The default-strategy matrix per scale. ``topology`` selects the built-in
# blueprint shape (``default_lz_design_for_scale``); ``cfw`` / ``centralized_
# logging`` / ``scp_guardrails`` toggle the governance extras the large tier
# adds; ``archetype_count`` (min, max) bounds how many archetypes the LLM may
# propose for that scale. ``estate_size`` carries the concrete sizing bands
# (servers / cores / memory / accounts) the estate is matched against — the
# operator sees their real estate vs the tier's band in the UI. The prose fields
# (philosophy / account_strategy / networking / security / audit / budgeting /
# identity_rule / target) are the comparison matrix the UI renders verbatim and
# the LLM is told to honor.
LZ_SCALE_TIERS: Dict[str, Dict[str, Any]] = {
    LZ_SMALL: {
        "scale": LZ_SMALL,
        "label": "Small — Start-up / Dev",
        "philosophy": "Speed & Simplicity",
        "target": "< 5 accounts; get to market quickly",
        "estate_size": {"servers": "< 50", "cores": "< 200",
                        "memory_gb": "< 800", "accounts": "1-2",
                        "apps": "< 5"},
        "account_strategy": "Single Master (billing) + 1-2 workload accounts",
        "networking": "Flat / simple VPCs; VPC Peering only if >1 VPC",
        "security": "Manual CAM, MFA on all users, Security Groups + Cloud "
                    "Security Center (basic malware/vuln scan)",
        "audit": "Local ActionTrail in the same account",
        "budgeting": "Manual review",
        "identity_rule": "CAM users + MFA; no long-term AK/SK for humans",
        "topology": "flat",
        "cfw": False,
        "centralized_logging": False,
        "scp_guardrails": False,
        "archetype_count": (2, 2),
    },
    LZ_MEDIUM: {
        "scale": LZ_MEDIUM,
        "label": "Medium — Growth / SME",
        "philosophy": "Control & Standardization",
        "target": "5-30 accounts; cross-departmental management",
        "estate_size": {"servers": "50 – 2,000", "cores": "200 – 8,000",
                        "memory_gb": "800 – 32,000", "accounts": "5-30",
                        "apps": "5 – 30"},
        "account_strategy": "Tencent Organizations + OUs (Prod / Staging / "
                            "Shared Services)",
        "networking": "Cloud Enterprise Network (CCN); all VPCs attached to "
                      "avoid peering meshes",
        "security": "Federated SSO (AD/LDAP); Config rules monitor common "
                    "misconfig (public buckets, unencrypted DBs)",
        "audit": "Centralized log bucket in a dedicated logging account",
        "budgeting": "Resource Groups + mandatory tagging; monthly report by "
                     "department",
        "identity_rule": "SSO roles only; no long-term AK/SK for humans",
        "topology": "hub_spoke",
        "cfw": False,
        "centralized_logging": True,
        "scp_guardrails": False,
        "archetype_count": (3, 4),
    },
    LZ_LARGE: {
        "scale": LZ_LARGE,
        "label": "Large — Enterprise / Global",
        "philosophy": "Governance & Automated Compliance",
        "target": "50+ accounts; regulatory compliance (ISO / SOC2 / local law)",
        "estate_size": {"servers": "> 2,000", "cores": "> 8,000",
                        "memory_gb": "> 32,000", "accounts": "50+",
                        "apps": "> 30"},
        "account_strategy": "Complex OU hierarchy (BU / Dept / Env) + Service "
                            "Control Policies (SCP) as OU guardrails",
        "networking": "Hub-and-Spoke over CCN + Cloud Firewall (CFW) deep "
                      "packet inspection in a dedicated network/firewall VPC",
        "security": "IdP federation (OIDC/SAML) + Policy-as-Code (OPA) "
                    "pre-deploy Terraform scan",
        "audit": "Global Security Account; SIEM integration + automated "
                 "remediation (e.g. auto-close a public port)",
        "budgeting": "Real-time dashboard + charge-back via Billing APIs into "
                     "the internal finance system",
        "identity_rule": "IdP federation, role-only, PaC-enforced; no "
                         "long-term AK/SK for humans",
        "topology": "hub_spoke_cfw",
        "cfw": True,
        "centralized_logging": True,
        "scp_guardrails": True,
        "archetype_count": (3, 6),
    },
}

# numeric thresholds behind the estate_size bands — used by scale_for so the
# inference matches the bands the UI shows (servers / cores / apps), not just
# host count. A fat-but-few estate (few hosts, many cores) still scales up.
_LZ_THRESH = {"small_servers": 50, "small_cores": 200, "small_apps": 5,
              "large_servers": 2000, "large_cores": 8000, "large_apps": 30}


def estate_scale_metrics(servers: Sequence[Server]) -> Dict[str, Any]:
    """The concrete estate sizing metrics the scale tier is matched against —
    server count, distinct apps, total vCPU cores, total memory (GB), baremetal
    count, regulated flag, and a by-env distribution. Computed in ONE pass over
    the estate and consumed by ``scale_for`` + the ``/api/lz/scale`` signals +
    the LLM estate context so they all share one source of truth. Pure, no DB."""
    app_ids: set = set()
    cores = 0
    mem_gb = 0
    baremetal = 0
    regulated = False
    envs: Dict[str, int] = {}
    for s in servers:
        for aid in (s.app_ids or []):
            app_ids.add(aid)
        cores += s.cpu_cores or 0
        mem_gb += s.mem_gb or 0
        if (s.source_type or "") == "baremetal":
            baremetal += 1
        for t in (s.tags or []):
            if t.lower().strip() in _DMZ_HINTS:
                regulated = True
        e = s.env or "unknown"
        envs[e] = envs.get(e, 0) + 1
    return {"servers": len(servers), "apps": len(app_ids), "cores": cores,
            "memory_gb": mem_gb, "baremetal": baremetal, "regulated": regulated,
            "by_env": dict(sorted(envs.items(), key=lambda kv: -kv[1]))}

# The 3 "crucial advice for ALL scales" rules — cross-scale invariants the
# validator surfaces (as warnings) regardless of tier. They are planning/
# governance guidance, not CIDR-correctness errors, so they WARN (shown to the
# operator + fed to the LLM) rather than hard-reject — the scale defaults
# auto-fill governance so a scale-aware design passes cleanly.
LZ_GOLDEN_RULES: List[Dict[str, str]] = [
    {"key": "identity",
     "rule": "No long-term IAM Access Keys (AK/SK) for human users — SSO/roles "
             "only. This one thing mitigates ~80% of security risk at every scale.",
     "validator": "requirements.governance.identity must declare SSO/role-only "
                  "(never long-term AK/SK for humans)"},
    {"key": "audit",
     "rule": "Store ActionTrail logs in a DIFFERENT account than where the "
             "activity occurs, so a compromised account cannot delete its own "
             "evidence. (Relaxed for single-account small scale.)",
     "validator": "requirements.governance.audit.destination_account must "
                  "differ from each workload account"},
    {"key": "network",
     "rule": "Plan IP space (CIDRs) as if you will be 10x larger than today — "
             "re-addressing a live VPC is one of the hardest tasks in cloud "
             "engineering.",
     "validator": "each archetype VPC CIDR should hold >= 10x its workload_count "
                  "(soft headroom warning)"},
]


def scale_for(servers: Sequence[Server], apps: Optional[Sequence[Any]] = None,
              scale_override: Optional[str] = None,
              metrics: Optional[Dict[str, Any]] = None) -> str:
    """Infer the LZ scale tier from the estate size (deterministic).

    The tier drives the default strategy (``LZ_SCALE_TIERS``). Heuristic, matched
    against the ``estate_size`` bands the UI shows (servers / cores / apps):
      * ``large``  — any regulated/sensitive tag, OR > 30 apps, OR > 2,000
                     servers, OR > 8,000 total cores (regulatory compliance +
                     scale need governance; a fat-but-few estate still scales);
      * ``medium`` — >= 5 apps, OR >= 50 servers, OR >= 200 cores;
      * ``small``  — otherwise (start-up / dev).
    ``scale_override`` wins when it is a known tier (the operator picked one in
    the UI). Pass precomputed ``metrics`` (``estate_scale_metrics``) to avoid a
    re-scan when the caller already has them. Pure over the estate — no DB, no LLM.
    """
    if scale_override in LZ_SCALE_TIERS:
        return scale_override
    m = metrics if metrics is not None else estate_scale_metrics(servers)
    n_apps = len(apps) if apps is not None else m["apps"]
    n_servers = m["servers"]
    cores = m["cores"]
    if (m["regulated"] or n_apps > _LZ_THRESH["large_apps"]
            or n_servers > _LZ_THRESH["large_servers"]
            or cores > _LZ_THRESH["large_cores"]):
        return LZ_LARGE
    if (n_apps >= _LZ_THRESH["small_apps"]
            or n_servers >= _LZ_THRESH["small_servers"]
            or cores >= _LZ_THRESH["small_cores"]):
        return LZ_MEDIUM
    return LZ_SMALL


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
        "summary": "Hub LZ — DC peering + intranet only; spokes reach DC via corp.",
        "vpc": {"name": "vpc-corp", "cidr": "10.10.0.0/16",
                "description": "corp hub VPC with DC direct-connect / VPN peering"},
        "peering": {"to_dc": True, "to_corp": None, "to_online": True,
                    "to_dmz": False,
                    "description": "hub: peers to DC + online spoke; NOT to dmz "
                                   "(isolation). Online reaches DC via corp transit."},
        "internet_egress": {"nat": True, "public_clb": False},
        "security_groups": ["sg-corp-web-intra", "sg-corp-db-intra"],
        "cam_roles": ["corp-workload-role", "corp-dc-link-role"],
        "tag_policy": {"cost_center": "required", "env": "required",
                       "archetype": "corp"},
        "policy_as_code": [
            "deny public CLB in corp VPC (intranet only)",
            "require DC peering healthy before workload deploy",
            "require cost_center + env tags on every resource",
            "deny spoke (online/dmz) direct peering to DC — DC reachable via corp hub only",
        ],
    },
    LZ_ONLINE: {
        "archetype": LZ_ONLINE,
        "summary": "Spoke LZ — public CLB + CDN + WAF in front; reaches DC via corp.",
        "vpc": {"name": "vpc-online", "cidr": "10.20.0.0/16",
                "description": "online spoke VPC with public ingress"},
        "peering": {"to_dc": False, "to_corp": True, "to_online": None,
                    "to_dmz": False,
                    "description": "spoke: peers to corp hub only (reaches DC via "
                                   "corp transit); no direct DC peering; not to dmz"},
        "internet_egress": {"nat": True, "public_clb": True, "cdn": True, "waf": True},
        "security_groups": ["sg-online-web-pub", "sg-online-app-intra"],
        "cam_roles": ["online-workload-role", "online-clb-manage-role"],
        "tag_policy": {"cost_center": "required", "env": "required",
                       "archetype": "online", "public_exposure": "required"},
        "policy_as_code": [
            "public CLB must sit behind WAF",
            "CDN origin must be online VPC only",
            "require public_exposure tag on every public resource",
            "deny direct online→DC peering; DC reachable via corp hub transit only",
        ],
    },
    LZ_DMZ: {
        "archetype": LZ_DMZ,
        "summary": "Isolated LZ — no direct internet, no peering (regulated).",
        "vpc": {"name": "vpc-dmz", "cidr": "10.30.0.0/16",
                "description": "dmz VPC — isolated, regulated workloads"},
        "peering": {"to_dc": False, "to_corp": False, "to_online": False,
                    "to_dmz": None,
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


def archetype_for(server: Server, match: Optional[Match] = None,
                  classifier: Optional[Dict[str, Any]] = None) -> str:
    """Classify a server into an LZ archetype (F8, deterministic).

    With no ``classifier`` (the built-in default design), precedence is:
      1. explicit tag (``idc:dmz`` / ``idc:online`` / ``idc:corp``) — operator
         override, à la Azure ``AzM.MigrationIntent``;
      2. regulated/sensitive hints in tags → ``dmz``;
      3. web/frontend role → ``online`` (internet-exposed);
      4. internal role (db/app/infra/…) → ``corp`` (the hub account is the
         built-in default's home for internal workloads — a deliberate strategy,
         not a catch-all);
      5. no signal at all (no role, no classifying tag) → ``LZ_PENDING`` — left
         for manual review, NOT dumped into ``corp``.

    With a ``classifier`` (an operator-authored / LLM-authored design that
    carries ``requirements.classifier`` — Phase B), the DESIGN owns the mapping:
      1. ``tag_map`` — any tag the server carries maps to its archetype (explicit
         operator override, wins over everything);
      2. ``app_map`` — an app_id the server hosts maps to its archetype (the
         multi-workload-account signal: "this account owns these apps");
      3. ``role_map`` — the server's role maps to its archetype;
      4. undetermined → ``LZ_PENDING`` (no rule matched; left for manual
         classification rather than absorbed into a catch-all account).
    The built-in tag/role hints are NOT consulted under a custom design: its
    archetype names are arbitrary (hub/spoke/pci/...), so the built-in
    corp/online/dmz fallbacks would map to names the design doesn't define. A
    ``default`` catch-all is deliberately NOT used: undetermined hosts are
    flagged ``pending`` so the operator reviews them instead of the classifier
    guessing an account (which used to silently fill the hub/corp account with
    leftover workloads). The LLM/operator should cover every real role + app via
    ``role_map`` / ``app_map`` so nothing falls through unintentionally.

    The ``Workload.tier`` ("frontend"/"web") is not available on the Server,
    so the role carries that signal (a frontend host has ``role=web``). Pure:
    same inputs -> same archetype, no DB, no LLM.
    """
    tags = {t.lower().strip() for t in (server.tags or [])}
    if classifier:
        tm = classifier.get("tag_map") or {}
        # iterate tags in a DETERMINISTIC order (sorted) so a server carrying
        # several mapped tags always resolves to the same archetype — iterating
        # the set directly is hash-order-dependent and would classify the same
        # server differently run-to-run. Sorted gives a stable, documented
        # precedence (lexicographic); conflicting tags should be avoided in the
        # design, but this guarantees reproducibility either way.
        for t in sorted(tags):
            if t in tm:
                return tm[t]
        # app_map: route by which app(s) the server hosts — the workload-account
        # signal. A server may host several apps; first mapped app wins (app_ids
        # is a stable stored list, so this is deterministic).
        am = classifier.get("app_map") or {}
        if am:
            for aid in (server.app_ids or []):
                if aid in am:
                    return am[aid]
        rm = classifier.get("role_map") or {}
        role = (server.role or "").lower()
        if role in rm:
            return rm[role]
        # nothing matched — undetermined. Left pending for manual review instead
        # of being absorbed into a catch-all (which used to silently fill the
        # hub/corp account with leftover workloads). ``default`` is intentionally
        # ignored: a fallback bucket is not a determination.
        return LZ_PENDING
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
    # an internal workload (any non-web role) belongs in the corp hub — the
    # built-in default design's strategy. A host with NO role and NO classifying
    # tag has no signal at all: genuinely undetermined → pending for review,
    # NOT dumped into the hub account.
    if role:
        return LZ_CORP
    return LZ_PENDING


def _peering_asymmetries(archetypes: Dict[str, Dict[str, Any]]
                         ) -> List[str]:
    """Return the list of asymmetric peering edges in an archetype set.

    Peering (VPC peering / VNet peering / CCN attachment) is bidirectional: if
    archetype A declares ``to_<B>: True`` then B must declare ``to_<A>: True``.
    A one-way declaration is invalid — the agent consumes the blueprint
    verbatim as Terraform context and would emit broken HCL. ``None``/missing
    means "self / unset" and is treated as False on the other side's check.

    Generalized over ANY archetype set (built-in or operator-authored design)
    so ``validate_lz_design`` and the import-time built-in check share one rule.
    """
    names = list(archetypes.keys())
    errors: List[str] = []
    for a in names:
        pa = (archetypes[a].get("peering") or {})
        for b in names:
            if a == b:
                continue
            va = pa.get(f"to_{b}")
            vb = (archetypes[b].get("peering") or {}).get(f"to_{a}")
            if bool(va) != bool(vb):
                errors.append(
                    f"asymmetric peering: {a}.to_{b}={va} but {b}.to_{a}={vb} "
                    "— peering must be bidirectional")
    return errors


def _assert_peering_symmetric() -> None:
    """Import-time check that the built-in ``LZ_BLUEPRINTS`` peering is a
    symmetric matrix (fail fast, not silently at apply). Delegates to the
    generalized ``_peering_asymmetries`` helper."""
    bad = _peering_asymmetries(LZ_BLUEPRINTS)
    if bad:
        raise ValueError(bad[0])


_assert_peering_symmetric()


# ---------------------------------------------------------------------------
# operator-authored LZ design (Phase A) — deterministic blueprint validator
# ---------------------------------------------------------------------------
_REQUIRED_ARCH_KEYS = ("vpc", "peering", "internet_egress", "security_groups",
                        "cam_roles", "tag_policy", "policy_as_code")


def _net(cidr: str) -> Optional[Any]:
    """Parse a CIDR string into an ip_network (strict=False), or None."""
    import ipaddress
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        return None


def validate_lz_design(design: "LZDesign") -> Dict[str, Any]:
    """Deterministic validation of an operator-authored LZ design (Phase A).

    The LLM (Phase B) proposes a blueprint; this validator is the
    deterministic gate that loops the LLM until the proposal is sound (the
    same propose→validate→loop pattern as ``seven_r_strategy``'s confidence
    clamp). It checks the things the LLM can get wrong and that would produce
    broken Terraform or an unworkable LZ:

      * at least one archetype;
      * every archetype has the required blueprint keys (vpc / peering /
        internet_egress / security_groups / cam_roles / tag_policy /
        policy_as_code) and ``policy_as_code`` is non-empty;
      * every archetype's ``vpc.cidr`` parses and does not overlap another
        archetype's VPC CIDR or any of the design's ``onprem_cidrs``;
      * peering is a symmetric matrix (``_peering_asymmetries``).

    The cross-scale ``LZ_GOLDEN_RULES`` (identity / audit / 10x-CIDR headroom)
    are surfaced as **warnings** (``warnings[]`` in the result) — they are
    governance/planning guidance, not CIDR-correctness errors, so they do not
    flip ``ok``. The scale defaults auto-fill ``requirements.governance`` so a
    scale-aware design passes cleanly; a hand-authored design without
    governance gets a warning pointing the operator at the missing rule.

    Returns ``{ok, errors[], warnings[]}``. Pure over the design — no DB, no LLM.
    """
    errs: List[str] = []
    warns: List[str] = []
    archs = design.archetypes or {}
    if not archs:
        return {"ok": False, "errors": ["design has no archetypes"],
                "warnings": []}
    for name, bp in archs.items():
        if not isinstance(bp, dict):
            errs.append(f"archetype {name!r}: blueprint must be an object")
            continue
        for k in _REQUIRED_ARCH_KEYS:
            if k not in bp:
                errs.append(f"archetype {name!r}: missing required key {k!r}")
        pol = bp.get("policy_as_code")
        if isinstance(pol, list) and not pol:
            errs.append(f"archetype {name!r}: policy_as_code must be non-empty")
        vpc = bp.get("vpc") or {}
        cidr = vpc.get("cidr") if isinstance(vpc, dict) else None
        if not cidr:
            errs.append(f"archetype {name!r}: vpc.cidr missing")
    # CIDR overlap (only among parseable CIDRs)
    nets: Dict[str, Any] = {}   # label -> ip_network
    for name, bp in archs.items():
        cidr = (bp.get("vpc") or {}).get("cidr")
        if not cidr:
            continue
        n = _net(cidr)
        if n is None:
            errs.append(f"archetype {name!r}: invalid vpc.cidr {cidr!r}")
            continue
        nets[f"VPC {name} ({cidr})"] = n
    for oc in (design.onprem_cidrs or []):
        n = _net(oc)
        if n is None:
            errs.append(f"invalid onprem_cidr {oc!r}")
            continue
        nets[f"on-prem {oc}"] = n
    labels = list(nets.keys())
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            if nets[labels[i]].overlaps(nets[labels[j]]):
                errs.append(f"CIDR overlap: {labels[i]} overlaps {labels[j]}")
    # peering symmetry (generalized)
    errs.extend(_peering_asymmetries(archs))
    # -- cross-scale golden-rule warnings (LZ_GOLDEN_RULES) -------------------
    # governance block at requirements.governance (auto-filled by the scale
    # defaults in design_lz; a hand-authored design may omit it).
    req = design.requirements if isinstance(design.requirements, dict) else {}
    gov = req.get("governance") if isinstance(req, dict) else None
    if not isinstance(gov, dict) or not gov:
        warns.append("governance block missing — the scale defaults (identity "
                     "SSO/role-only, cross-account ActionTrail, 10x CIDR "
                     "headroom) are not recorded; run a scale-aware interview "
                     "or add requirements.governance")
    else:
        ident = gov.get("identity") or {}
        if isinstance(ident, dict) and ident.get("mode") in (
                "long_term_aksk", "aksk"):
            warns.append("identity rule violated: long-term AK/SK for humans — "
                         "use SSO/roles only (mitigates ~80% of security risk)")
        elif not isinstance(ident, dict) or not ident.get("mode"):
            warns.append("identity rule: requirements.governance.identity.mode "
                         "is unset — should be sso_role_only")
        audit = gov.get("audit") or {}
        scale = (gov.get("scale") or req.get("scale") or LZ_MEDIUM)
        centralized = (LZ_SCALE_TIERS.get(scale) or {}).get(
            "centralized_logging", True)
        if centralized and isinstance(audit, dict) and audit.get("cross_account") \
                and audit.get("destination_account") in (None, "", "self"):
            warns.append("audit rule: centralized logging tier requires a "
                         "destination_account separate from the workload "
                         "accounts (a compromised account can't delete its own "
                         "ActionTrail evidence)")
    # 10x CIDR headroom: each archetype VPC should hold >= 10x the estate
    # servers it will host. workload_count is unknown to the validator (it has
    # the design, not the estate), so check the VPC itself can hold >= 10x a
    # reasonable floor — at minimum the CIDR should be sized to leave headroom.
    # We warn when a VPC CIDR is so small it can't hold 10x even a modest
    # workload (e.g. a /28 holding 14 addresses for a tier with servers).
    for name, bp in archs.items():
        cidr = (bp.get("vpc") or {}).get("cidr")
        n = _net(cidr) if cidr else None
        if n is None:
            continue
        hosts = n.num_addresses - 2 if n.num_addresses > 2 else n.num_addresses
        # 10x rule of thumb: a VPC smaller than /24 (254 hosts) for any real
        # workload tier leaves no 10x headroom. /24 is the floor for a workload VPC.
        if hosts < 250:
            warns.append(f"network rule: {name} VPC {cidr} holds only {hosts} "
                         f"addresses — plan IP space for ~10x growth "
                         f"(workload VPCs should be /24 or larger)")
    return {"ok": not errs, "errors": errs, "warnings": warns}


def _governance_for_scale(scale: str) -> Dict[str, Any]:
    """The default governance block (identity / audit / network) for a scale
    tier — the ``LZ_GOLDEN_RULES`` instantiated with the tier's defaults. Lives
    at ``requirements.governance`` on the design so the validator can check the
    cross-scale invariants and the UI can render them. The operator (via the AI
    prompt) can override any sub-key; the scale defaults fill the rest."""
    tier = LZ_SCALE_TIERS.get(scale) or LZ_SCALE_TIERS[LZ_MEDIUM]
    centralized = tier["centralized_logging"]
    return {
        "scale": scale,
        "identity": {
            "mode": "sso_role_only",
            "rule": "no long-term AK/SK for human users (SSO/roles only)",
            "federation": "idp_oidc_saml" if scale == LZ_LARGE else (
                "federated_ad_ldap" if scale == LZ_MEDIUM else "cam_mfa"),
        },
        "audit": {
            "actiontrail": True,
            "destination_account": "logging" if centralized else "self",
            "cross_account": centralized,
            "siem": scale == LZ_LARGE,
            "automated_remediation": scale == LZ_LARGE,
            "rule": ("ActionTrail logs land in a separate logging account" if
                     centralized else "ActionTrail local to the account (small)"),
        },
        "network": {
            "planning": "10x headroom on every VPC CIDR",
            "cfw_inspection": tier["cfw"],
            "topology": tier["networking"],
        },
        "budgeting": tier["budgeting"],
        "scp_guardrails": tier["scp_guardrails"],
    }


def _blueprints_for_scale(scale: str) -> Dict[str, Dict[str, Any]]:
    """The built-in archetype blueprints for a scale tier's topology shape.
    ``medium`` = the classic corp/online/dmz hub-and-spoke (``LZ_BLUEPRINTS``);
    ``large`` = that + Cloud Firewall inspection + SCP-style guardrails added
    to each archetype's ``policy_as_code``; ``small`` = a flat 2-archetype
    layout (online + corp) with simple peering and no isolated dmz tier. The
    operator customizes app placement / CIDRs / names via the AI prompt — these
    are the defaults the scale implies."""
    if scale == LZ_SMALL:
        return {
            LZ_CORP: {
                **LZ_BLUEPRINTS[LZ_CORP],
                "summary": "Workload + DC-peering VPC (flat, no hub transit).",
                "peering": {"to_dc": True, "to_online": True, "to_dmz": None,
                            "to_corp": None,
                            "description": "flat: peers to DC + online; no hub "
                                           "transit, no dmz tier (small scale)"},
                "internet_egress": {"nat": True, "public_clb": False},
            },
            LZ_ONLINE: {
                **LZ_BLUEPRINTS[LZ_ONLINE],
                "summary": "Public web/app VPC (flat, internet-exposed).",
                "peering": {"to_dc": False, "to_corp": True, "to_dmz": None,
                            "to_online": None,
                            "description": "flat: peers to corp only; no dmz tier"},
            },
        }
    bp = {a: {**LZ_BLUEPRINTS[a]} for a in LZ_ARCHETYPES}
    if scale == LZ_LARGE:
        # large tier adds Cloud Firewall inspection + SCP-style guardrails to
        # every archetype's policy_as_code (the governance extras the matrix
        # calls out for enterprise). Kept as policy strings the executor turns
        # into Terraform context, like every other policy_as_code entry.
        for name, arch in bp.items():
            pol = list(arch.get("policy_as_code") or [])
            pol.append("all inter-VPC traffic traverses Cloud Firewall (CFW) "
                       "for deep packet inspection")
            pol.append("SCP guardrail: deny any user (even admin) from deleting "
                       "ActionTrail logs or changing security config in prod")
            arch["policy_as_code"] = pol
        bp[LZ_CORP]["summary"] = ("Hub LZ — DC peering + CFW inspection transit; "
                                  "spokes reach DC via corp.")
    return bp


def default_lz_design_for_scale(scale: str) -> "LZDesign":
    """The built-in LZ design for a scale tier — the default-strategy blueprint
    (topology + governance) the operator gets before they author anything via
    the AI prompt. ``scale`` defaults to ``medium`` when unknown so callers
    that hand in a bad value keep working. Marked ``is_active=False`` (a
    fallback, not a stored row)."""
    from .models import LZDesign
    scale = scale if scale in LZ_SCALE_TIERS else LZ_MEDIUM
    tier = LZ_SCALE_TIERS[scale]
    return LZDesign(
        design_id=f"builtin-{scale}",
        name=f"Built-in {tier['label']}",
        archetypes=_blueprints_for_scale(scale),
        requirements={"scale": scale, "governance": _governance_for_scale(scale),
                      "defaults_applied": tier, "notes": tier["philosophy"]},
        conversation=[], onprem_cidrs=[], is_active=False, scale=scale,
    )


def default_lz_design() -> "LZDesign":
    """The built-in ``LZ_BLUEPRINTS`` wrapped as an ``LZDesign`` — the default
    design used when no active operator-authored design exists. Marked
    ``is_active=False`` so it never collides with a real persisted design; it
    is a fallback, not a stored row. Equivalent to ``default_lz_design_for_
    scale(MEDIUM)`` — kept for back-compat with callers/tests that predate the
    scale tiers."""
    return default_lz_design_for_scale(LZ_MEDIUM)


# ---------------------------------------------------------------------------
# active-design resolution — built-in default vs operator-authored design
# ---------------------------------------------------------------------------
def get_active_lz_design(store, scale: Optional[str] = None) -> "LZDesign":
    """The LZ design that drives emit + gate right now: the operator's active
    design if one is persisted, else the built-in default for ``scale`` (or
    ``medium`` when unset — back-compat). Never returns None — the built-in
    blueprint is always the floor, so callers (and the existing tests) keep
    working when no design has been authored yet. Pass the estate's inferred
    scale (``scale_for``) so the fallback matches the estate's maturity."""
    if hasattr(store, "get_active_lz_design"):
        d = store.get_active_lz_design()
        if d is not None and d.archetypes:
            return d
    return default_lz_design_for_scale(scale or LZ_MEDIUM)


def active_lz_archetypes(store) -> List[str]:
    """The archetype names of the active design (ordered), falling back to the
    built-in ``LZ_ARCHETYPES``. This replaces the hardcoded ``LZ_ARCHETYPES``
    tuple as the source of the archetype SET at every callsite that drives emit
    or gating (``lz_readiness``, ``check_lz_gate``, ``/api/lz/archetypes``,
    ``lz_context``, ``iac-emit``)."""
    return list(get_active_lz_design(store).archetypes.keys())


def _classifier_from_design(design: "LZDesign") -> Optional[Dict[str, Any]]:
    """The design-aware classifier (Phase B), or None when the design doesn't
    carry one (the built-in default, or a hand-authored design with no
    ``requirements.classifier``). None → callers use the built-in
    ``archetype_for`` tag/role logic, so every existing test + the built-in
    estate keep working unchanged.

    The classifier lives at ``design.requirements.classifier`` as
    ``{tag_map: {tag: archetype}, role_map: {role: archetype},
    app_map: {app_id: archetype}}``. Entries that point at archetypes the design
    doesn't define are filtered out (defensive — a stale classifier after an
    archetype was renamed must not map servers into a non-existent tier). Returns
    None when nothing usable remains.

    A ``default`` catch-all is intentionally NOT propagated: undetermined hosts
    (no tag/app/role rule matched) are flagged ``LZ_PENDING`` by
    ``archetype_for`` for manual review, never absorbed into a fallback account
    (see ``archetype_for``). A stored design that still carries a ``default`` key
    is tolerated here — the key is simply dropped on read.
    """
    if design is None:
        return None
    req = design.requirements or {}
    clf = req.get("classifier") if isinstance(req, dict) else None
    if not isinstance(clf, dict):
        return None
    archs = design.archetypes or {}
    tm = {k: v for k, v in (clf.get("tag_map") or {}).items() if v in archs}
    rm = {k: v for k, v in (clf.get("role_map") or {}).items() if v in archs}
    am = {k: v for k, v in (clf.get("app_map") or {}).items() if v in archs}
    if not tm and not rm and not am:
        return None
    return {"tag_map": tm, "role_map": rm, "app_map": am}


def active_classifier(store) -> Optional[Dict[str, Any]]:
    """The active LZ design's classifier (Phase B), or None for the built-in
    default — the single place callers resolve the design-aware classification
    map so ``lz_readiness`` / ``check_lz_gate`` / ``/api/lz/archetypes`` share
    one source of truth."""
    return _classifier_from_design(get_active_lz_design(store))


def resolve_archetype_blueprint(store, archetype: str) -> Optional[Dict[str, Any]]:
    """One archetype's blueprint from the active design, or None if the
    archetype isn't in the active design."""
    return get_active_lz_design(store).archetypes.get(archetype)


def resolve_all_archetype_blueprints(store) -> Dict[str, Dict[str, Any]]:
    """All archetype blueprints of the active design (the full ``archetypes``
    dict). Used by ``/api/lz/archetypes`` and ``lz_context_all``."""
    return get_active_lz_design(store).archetypes


def archetypes_for_servers(servers: Sequence[Server],
                           matches: Optional[Sequence[Match]] = None,
                           classifier: Optional[Dict[str, Any]] = None
                           ) -> Dict[str, str]:
    """server_id -> archetype for a batch. The match is optional (archetype_for
    currently keys off server signals only, but the signature reserves the
    match for future target-region-aware placement). ``classifier`` (Phase B)
    switches to design-aware classification; None = built-in behavior."""
    m_by_id = {m.server_id: m for m in (matches or [])}
    return {s.id: archetype_for(s, m_by_id.get(s.id), classifier) for s in servers}


# ---------------------------------------------------------------------------
# readiness — is an archetype's LZ finalized? (placement gate)
# ---------------------------------------------------------------------------
def lz_readiness(store, servers: Sequence[Server],
                 matches: Optional[Sequence[Match]] = None
                 ) -> Dict[str, Dict[str, Any]]:
    """Per-archetype LZ readiness, read from the persisted LZ status (F8) and
    the latest IaC artifact's guardrail verdict (F5).

    An archetype is ``ready`` only when its LZ Terraform has been applied +
    finalized (operator marks it via ``Store.set_lz_status``) AND — when an
    IaC artifact exists for ``lz:<arch>`` — that artifact's guardrails pass.
    No artifact → the finalized flag alone decides (back-compat: the guardrail
    check only ever ADDS a gate, never weakens the existing one). Default is
    ``not_ready`` so workload waves are gated until the target LZ is explicitly
    stood up — the safe default.

    Returns ``{archetype: {status, ready, workload_count, guardrail_pass,
    guardrail_summary}}`` where ``workload_count`` is how many estate servers
    target that archetype (for the UI / F10 readiness heatmap).
    """
    from .codeintel import check_iac_guardrails, iac_guardrail_summary
    archetypes = active_lz_archetypes(store)
    classifier = active_classifier(store)
    arch_of = archetypes_for_servers(list(servers), matches, classifier)
    counts = {a: 0 for a in archetypes}
    for a in arch_of.values():
        if a in counts:
            counts[a] += 1
        else:
            counts[a] = counts.get(a, 0) + 1
    statuses = store.list_lz_status()
    out: Dict[str, Dict[str, Any]] = {}
    for arch in archetypes:
        st = statuses.get(arch, LZ_NOT_READY)
        artifact = store.get_iac_artifact(f"lz:{arch}") if hasattr(
            store, "get_iac_artifact") else None
        gv = check_iac_guardrails(artifact)
        finalized = st == LZ_FINALIZED
        # guardrails only add a gate: ready requires finalized AND (no artifact
        # OR guardrails pass). No artifact → finalized alone (back-compat).
        guardrail_ok = gv["ok"]
        ready = finalized and (gv["no_artifact"] or guardrail_ok)
        out[arch] = {"status": st, "ready": ready,
                     "workload_count": counts.get(arch, 0),
                     "guardrail_pass": None if gv["no_artifact"] else guardrail_ok,
                     "guardrail_summary": iac_guardrail_summary(artifact),
                     "guardrail_failing": len(gv["failing"])}
    return out


def wave_archetypes(wave_server_ids: Sequence[str], servers: Sequence[Server],
                    matches: Optional[Sequence[Match]] = None,
                    classifier: Optional[Dict[str, Any]] = None,
                    archetype_names: Optional[Sequence[str]] = None
                    ) -> Dict[str, int]:
    """The archetype mix of a wave's members (for the UI + the placement gate).

    Wave members not present in ``servers`` (stale refs / dropped hosts) are
    skipped — classifying a phantom host as ``corp`` would silently add a corp
    entry to the mix and gate the wave on a non-existent workload.

    ``archetype_names`` (Phase B) keys the mix to the active design's archetype
    set (defaults to the built-in ``LZ_ARCHETYPES`` for back-compat); ``classifier``
    switches the per-server classification to design-aware. A custom-named
    design (hub/spoke/pci) thus shows a real workload mix instead of all-zero
    counts (the built-in classifier can't produce its archetype names).
    """
    names = list(archetype_names) if archetype_names else list(LZ_ARCHETYPES)
    srv_by_id = {s.id: s for s in servers}
    arch_of = archetypes_for_servers(list(srv_by_id.values()), matches, classifier)
    mix: Dict[str, int] = {a: 0 for a in names}
    for sid in wave_server_ids:
        if sid not in arch_of:
            continue  # stale wave member — don't fabricate a corp placement
        a = arch_of[sid]
        mix[a] = mix.get(a, 0) + 1
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
    archetypes = active_lz_archetypes(store)
    classifier = active_classifier(store)
    mix = wave_archetypes(wave.server_ids or [], servers, matches, classifier,
                          archetypes)
    readiness = lz_readiness(store, servers, matches)
    blocking = [a for a in archetypes
                if mix.get(a, 0) > 0 and not readiness.get(a, {}).get("ready")]
    # undetermined members: a host the classifier couldn't place (``pending``)
    # has no LZ, so the wave can't safely launch until the operator classifies
    # it (via a tag / app_map / role_map rule, or a VPC pin). Surfaced as its own
    # blocking category so it isn't lost under the not-finalized/guardrail split.
    pending_n = mix.get(LZ_PENDING, 0)
    pending_blocked = pending_n > 0
    # split the blocking reason: not-finalized vs guardrails-failing (F5)
    not_finalized = [a for a in blocking
                     if readiness[a]["status"] != LZ_FINALIZED]
    guardrail_blocked = [a for a in blocking
                        if readiness[a]["status"] == LZ_FINALIZED
                        and readiness[a]["guardrail_pass"] is False]
    reasons = []
    if pending_blocked:
        reasons.append(f"{pending_n} host(s) undetermined — classify before launch")
    if not_finalized:
        reasons.append(f"LZ not finalized: {', '.join(not_finalized)}")
    if guardrail_blocked:
        reasons.append(f"guardrails failing: {', '.join(guardrail_blocked)}")
    blocked = bool(blocking) or pending_blocked
    return {"ok": not blocked, "blocking_archetypes": blocking,
            "archetype_mix": mix,
            "not_finalized": not_finalized,
            "guardrail_blocked": guardrail_blocked,
            "pending": pending_n,
            "reason": ("all target archetypes ready" if not blocked
                       else "; ".join(reasons))}


__all__ = [
    "LZ_CORP", "LZ_ONLINE", "LZ_DMZ", "LZ_ARCHETYPES", "LZ_PENDING",
    "LZ_NOT_READY", "LZ_APPLIED", "LZ_FINALIZED", "LZ_STATUSES",
    "TAG_DMZ", "TAG_ONLINE", "TAG_CORP",
    "LZ_BLUEPRINTS",
    "LZ_SMALL", "LZ_MEDIUM", "LZ_LARGE", "LZ_SCALE_TIERS_ORDER",
    "LZ_SCALE_TIERS", "LZ_GOLDEN_RULES", "scale_for", "estate_scale_metrics",
    "default_lz_design_for_scale", "_governance_for_scale", "_blueprints_for_scale",
    "archetype_for", "archetypes_for_servers",
    "lz_readiness", "wave_archetypes", "check_lz_gate",
    "validate_lz_design", "default_lz_design",
    "active_lz_archetypes", "get_active_lz_design", "resolve_archetypes",
    "active_classifier", "_classifier_from_design",
]