"""F8 (Phase B+) — the network designer: VPC + subnets per account + placement.

Sits on top of the LZ design (Phase A/B). The LZ design gives each archetype a
VPC cidr; this module CARVES each VPC into subnets (one per tier — web/app/data/
infra) and PLACES every server into a VPC + subnet + IP. Each archetype IS one
account (the LZ design owns account naming/merging — Layer 1); the network
designer does NOT rename or merge accounts, it only carves subnets + places hosts
(Layer 2).

The LLM (``LLMClient.design_network_policy``) may PROPOSE the carving policy
(subnet size, tier grouping); this module is the deterministic engine that
INSTANTIATES + VALIDATES it (subnets within the VPC, non-overlapping, IP within
the subnet, capacity not exceeded). Same propose→validate→loop split as the LZ
design / wave planner: the LLM proposes, the deterministic engine is the gate.
With no LLM, ``default_network_policy`` is the workhorse (works out of the box on
any estate).

IP-exhaustion prevention: when ``subnet_prefix`` is auto, each tier subnet is
SIZED FROM ITS ACTUAL HOST DEMAND (``_prefix_for_count`` — ~2x headroom, floored
at a /28 so an empty tier still has a real subnet) instead of carved into equal
blocks, so a tier with many hosts gets a large block and a near-empty tier gets a
small one. Carving is largest-first (first-fit-decreasing → minimal
fragmentation). A proactive capacity check then flags any tier whose demand
exceeds its subnet's usable count — naming the tier/VPC and the Layer-1 fix
(widen the VPC cidr in the LZ design) — BEFORE silent per-server overflow.

Placements are keyed by STABLE hostname (lowercased) — NOT the rebuild-changing
``Server.id`` — so a placement survives a rebuild (the server row gets a new id
but the same hostname). Per-server subnet overrides (the operator moving a
server to a different subnet) ride the design and survive a regenerate.
"""
from __future__ import annotations

import ipaddress
import math
from typing import Any, Dict, List, Optional, Sequence

from .lz import LZ_CORP, LZ_PENDING, _classifier_from_design, archetype_for
from .models import Match, NetworkDesign, Server

# ---------------------------------------------------------------------------
# tiers — the subnet grouping inside a VPC (one subnet per tier)
# ---------------------------------------------------------------------------
TIER_WEB = "web"
TIER_APP = "app"
TIER_DATA = "data"
TIER_INFRA = "infra"
TIER_ORDER = (TIER_WEB, TIER_APP, TIER_DATA, TIER_INFRA)

# role -> tier (the subnet a server lands in within its archetype VPC). Roles
# not listed default to ``app`` (the generic workload tier).
_TIER_MAP: Dict[str, str] = {
    "web": TIER_WEB, "frontend": TIER_WEB,
    "app": TIER_APP, "api": TIER_APP, "general": TIER_APP, "service": TIER_APP,
    "db": TIER_DATA, "cache": TIER_DATA, "hadoop": TIER_DATA, "database": TIER_DATA,
    "k8s": TIER_INFRA, "monitoring": TIER_INFRA, "infra": TIER_INFRA,
}


def tier_of(role: str, tier_map: Optional[Dict[str, str]] = None) -> str:
    """Map a server role to a placement tier (subnet group within its VPC).

    ``tier_map`` (optional) overrides/augments the built-in role→tier map — the
    network policy's ``tiers`` field, merged over the defaults by the caller so
    the operator/LLM tier grouping actually takes effect."""
    r = (role or "").lower().strip()
    if tier_map and r in tier_map:
        return tier_map[r]
    return _TIER_MAP.get(r, TIER_APP)


# ---------------------------------------------------------------------------
# PaaS-aware sizing — managed-service targets don't each take a tier-subnet IP
# ---------------------------------------------------------------------------
# A source host matched to a MANAGED product becomes a cloud-managed instance
# (TencentDB / TData / object-or-file storage) — it does NOT land as a CVM in a
# carved web/app/data/infra subnet, so it consumes NO per-host subnet IP. The
# deterministic engine therefore EXCLUDES these from the per-tier host demand
# (so the CIDR is sized for the real CVM/BM compute, not inflated by PaaS) and
# places them with ip="" + managed=True (they still belong to an account/VPC).
#
# TKE / EMR are managed COMPUTE clusters whose nodes/pods DO live in VPC subnets,
# so they stay "compute" (counted + placed with an IP) — counting their source
# hosts is a conservative over-estimate that prevents under-sizing the cluster's
# subnet. Add more managed products here as the PaaS detector grows.
MANAGED_PRODUCTS = {
    "CDB", "TData", "COS", "CFS",
    "TencentDB for PostgreSQL", "TencentDB for SQL Server",
    "TencentDB for MongoDB", "TencentDB for Redis",
}


def _is_managed_target(server: "Server",
                       match_by_sid: Optional[Dict[str, "Match"]]) -> bool:
    """True when the server's match target is a managed/PaaS product (no
    per-host tier-subnet IP). No match -> defaults to a CVM (place + count), so
    callers without matches keep the legacy all-placed behaviour."""
    if not match_by_sid:
        return False
    m = match_by_sid.get(server.id)
    if m is None:
        return False
    return (m.target.product or "").strip() in MANAGED_PRODUCTS


def default_network_policy() -> Dict[str, Any]:
    """The workhorse carving policy (used when the LLM is off / not asked).

    ``subnet_prefix`` ``None`` = auto: each tier subnet is SIZED FROM ITS ACTUAL
    HOST DEMAND (``_prefix_for_count`` — ~2x headroom, floored at /28) rather
    than carved into equal blocks, so a tier with many hosts gets a large block
    and a near-empty tier gets a small one (prevents IP exhaustion on lopsided
    estates). The operator / LLM may override with an explicit prefix (then all
    tiers get equal /prefix blocks). ``tiers`` overrides the role→tier map.
    Account naming/merging is NOT a network-policy knob — it is Layer 1 (the LZ
    design): the account name IS the archetype name.
    """
    return {"subnet_prefix": None, "tier_order": list(TIER_ORDER),
            "tiers": dict(_TIER_MAP)}


# ---------------------------------------------------------------------------
# subnet carving
# ---------------------------------------------------------------------------
# auto-sizing knobs — see ``_prefix_for_count`` / ``carve_subnets_sized``.
MIN_TIER_PREFIX = 28        # floor: every tier gets at least a /28 (14 usable)
DEFAULT_HEADROOM = 2.0      # auto subnets hold ~2x the current host count


def _vpc_net(cidr: str) -> Optional[ipaddress._BaseNetwork]:
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        return None


def _prefix_for_count(count: int, headroom: float = DEFAULT_HEADROOM) -> int:
    """Smallest subnet prefix (largest block) that holds ``count`` hosts with
    ``headroom`` growth, plus room for the network/broadcast/gateway reserved
    addresses. Floored at ``MIN_TIER_PREFIX`` so a small or empty tier still
    gets a usable /28 (14 hosts) instead of a /30 with 1 assignable host.

    ``count`` is the number of servers the tier must hold. Returns a prefix
    length (e.g. 18 for ~16k hosts, 28 for a handful)."""
    need = max(2, math.ceil(count * headroom)) + 3   # +net +bcast +gateway
    bits = max(1, math.ceil(math.log2(need)))
    return min(32 - bits, MIN_TIER_PREFIX)


def carve_subnets(vpc_cidr: str, tier_names: Sequence[str],
                  subnet_prefix: Optional[int] = None
                  ) -> List[Dict[str, Any]]:
    """Carve ``vpc_cidr`` into one EQUAL-sized subnet per tier name (in order).

    ``subnet_prefix`` ``None`` = auto (``vpc_prefix + 2`` → 4 equal subnets of
    the largest size that leaves 4). Subnets are taken in order from the VPC's
    address space, so they are non-overlapping and within the VPC by
    construction. Tiers beyond what fits in the VPC are dropped (the caller's
    validation reports the missing tier). Each subnet reserves its first host
    as the gateway (``.1``); placement assigns from the rest.

    Use ``carve_subnets_sized`` instead when each tier should be sized to its
    own host demand (the auto path in ``build_network_design``).
    """
    net = _vpc_net(vpc_cidr)
    if net is None or not tier_names:
        return []
    pfx = int(subnet_prefix) if subnet_prefix else (net.prefixlen + 2)
    if pfx <= net.prefixlen:
        pfx = net.prefixlen + 1     # need at least 2 subnets worth of room
    if pfx > 32:
        return []
    # carve the whole VPC into /pfx blocks and take the first N (one per tier)
    blocks = net.subnets(new_prefix=pfx)
    out: List[Dict[str, Any]] = []
    for tier in tier_names:
        try:
            sn = next(blocks)
        except StopIteration:
            break       # VPC can't hold this many subnets — caller validates
        out.append(_subnet_dict(tier, sn))
    return out


def carve_subnets_sized(vpc_cidr: str,
                        tier_prefixes: Sequence[tuple]
                        ) -> List[Dict[str, Any]]:
    """Carve ``vpc_cidr`` into one subnet per ``(name, prefix)`` — MIXED sizes,
    each tier sized to its own demand. Blocks are taken sequentially from the
    VPC's address space (non-overlapping + within the VPC by construction) and
    carved LARGEST-first (first-fit-decreasing → minimal fragmentation), then
    the result is reordered into the caller's tier order for a stable display.

    Tiers whose block doesn't fit in the remaining VPC space are dropped; the
    caller's validation reports the missing tier + the Layer-1 fix (widen the
    VPC cidr). Each subnet reserves its first host as the gateway.
    """
    net = _vpc_net(vpc_cidr)
    if net is None or not tier_prefixes:
        return []
    netbits = net.prefixlen
    # keep only tiers whose prefix fits inside the VPC; remember tier order
    specs = [(name, pfx, i) for i, (name, pfx) in enumerate(tier_prefixes)
             if isinstance(pfx, int) and netbits <= pfx <= 32]
    # largest blocks first (smallest prefix), tiebreak by tier order (stable)
    specs.sort(key=lambda t: (t[1], t[2]))
    AddrCls = ipaddress.IPv4Address if net.version == 4 else ipaddress.IPv6Address
    NetCls = ipaddress.IPv4Network if net.version == 4 else ipaddress.IPv6Network
    cur = int(net.network_address)
    end = int(net.broadcast_address)
    carved: Dict[str, Dict[str, Any]] = {}
    for name, pfx, _i in specs:
        block = 1 << (net.max_prefixlen - pfx)   # addresses in a /pfx block
        if cur + block - 1 > end:
            continue                              # no room for this tier — drop
        sn = NetCls(f"{AddrCls(cur)}/{pfx}", strict=False)
        cur += block
        carved[name] = _subnet_dict(name, sn)
    # return in the caller's tier order, skipping tiers that didn't fit
    return [carved[name] for (name, _pfx) in tier_prefixes if name in carved]


def _subnet_dict(tier: str, sn: ipaddress._BaseNetwork) -> Dict[str, Any]:
    """The per-subnet record shared by ``carve_subnets`` / ``carve_subnets_sized``."""
    hosts = list(sn.hosts())        # .1..last-1 for a /24; skips net+bcast
    gateway = str(hosts[0]) if hosts else ""
    return {
        "name": tier, "cidr": str(sn), "tier": tier, "az": 1,
        "capacity": max(0, sn.num_addresses - 2),     # minus net + bcast
        "usable": max(0, len(hosts) - 1),             # minus gateway
        "used": 0, "gateway": gateway,
        "network": str(sn.network_address),
        "netmask": str(sn.netmask),
    }


def _account_for(archetype: str, policy: Dict[str, Any]) -> str:
    """The account name for an archetype. Account naming/merging is a Layer-1
    concern (the LZ design) — the network designer does NOT rename or merge
    accounts, so the account name IS the archetype name. Any ``account_map``
    in a legacy/LLM-emitted policy is ignored (kept in the signature for
    back-compat with stored policies)."""
    return archetype


# ---------------------------------------------------------------------------
# build — carve VPCs + place every server
# ---------------------------------------------------------------------------
def build_network_design(lz_design, servers: Sequence[Server],
                          matches: Optional[Sequence[Match]] = None,
                          policy: Optional[Dict[str, Any]] = None,
                          overrides: Optional[Dict[str, str]] = None,
                          name: str = "", design_id: str = "",
                          archetype_overrides: Optional[Dict[str, str]] = None,
                          account_overrides: Optional[Dict[str, str]] = None
                          ) -> NetworkDesign:
    """Carve the active LZ design's per-archetype VPCs into subnets and place
    every server into a VPC + subnet + IP. Deterministic over (lz_design,
    servers, policy, overrides, archetype_overrides) — same inputs => same
    topology.

    ``lz_design`` is the active LZ design (Phase A/B) — its ``archetypes`` give
    each archetype a VPC cidr; its ``requirements.classifier`` drives
    per-server archetype placement. ``policy`` is the carving policy
    (``default_network_policy`` when None). ``overrides`` is
    ``{hostname: subnet_name}`` — operator pins that move a server to a
    specific subnet within its VPC (survive regenerate). ``archetype_overrides``
    is ``{hostname: archetype}`` — operator pins that move a server to a
    DIFFERENT VPC (VPC is 1:1 with archetype, so this reclassifies the server
    for placement purposes without touching its inventory tags / the LZ
    classifier). An archetype pin clears any stale subnet pin (subnet names
    differ across VPCs); the server then lands in its tier subnet in the new
    VPC unless ``overrides`` also names a subnet valid in the new VPC.
    ``account_overrides`` is ``{hostname: account}`` — operator pins that
    reattribute a server to a DIFFERENT account WITHOUT moving its VPC (account
    is otherwise derived 1:1 from the archetype). The value must be one of the
    accounts already in the design; the placement endpoint validates that.

    Servers are placed in a stable order (by hostname) so the IP assignment is
    reproducible. Each subnet's first host is the gateway (reserved); IPs are
    assigned sequentially from the second host. A subnet that fills up reports
    overflow in ``validation`` (the server is placed with ``ip=""``).
    """
    policy = policy or default_network_policy()
    overrides = overrides or {}
    arch_overrides = archetype_overrides or {}
    acct_overrides = account_overrides or {}
    archs = (lz_design.archetypes or {}) if lz_design is not None else {}
    classifier = _classifier_from_design(lz_design)
    # PaaS-aware sizing: a host matched to a managed/PaaS product (TencentDB /
    # TData / COS / CFS) does NOT consume a per-host tier-subnet IP, so it is
    # excluded from the demand count (CIDR sized for real CVM/BM compute only)
    # and placed with ip="" + managed=True below.
    match_by_sid = {m.server_id: m for m in (matches or [])} if matches else None
    tier_order = policy.get("tier_order") or TIER_ORDER
    # effective role->tier map: the policy's ``tiers`` override merged over the
    # built-in map (lowercased role keys). The engine consumes this — without it
    # the operator/LLM tier grouping was silently ignored (build used the module
    # map directly), so a policy that grouped db+cache into "data" had no effect.
    tier_map = dict(_TIER_MAP)
    _tiers_pol = policy.get("tiers") or {}
    if isinstance(_tiers_pol, dict):
        for k, v in _tiers_pol.items():
            if isinstance(v, str):
                tier_map[str(k).lower().strip()] = v
    # the fallback tier for a role whose mapped tier isn't carved in this VPC —
    # happens whenever the operator/LLM uses CUSTOM tier names (e.g. tier_order
    # ["public","private","protected"]) but a role still resolves to a built-in
    # name ("app"/"data") via _TIER_MAP. Without this, that role lands in a
    # subnet that doesn't exist → ip="" / unplaced. The policy may name a
    # default_tier; else the generic "app" if present, else the 2nd tier, else
    # the 1st — so there is always a valid landing tier.
    _tier_set = set(tier_order)
    _default_tier = policy.get("default_tier")
    if not (isinstance(_default_tier, str) and _default_tier in _tier_set):
        _default_tier = (TIER_APP if TIER_APP in _tier_set
                         else (tier_order[1] if len(tier_order) > 1 else tier_order[0]))
    explicit_prefix = policy.get("subnet_prefix")
    onprem = list(getattr(lz_design, "onprem_cidrs", []) or [])

    # --- demand pass: count servers per (archetype, tier) so the auto carver
    # can size each tier subnet to its actual host count (prevents IP exhaustion
    # on lopsided estates — a tier with many hosts gets a large block, a near-
    # empty tier gets a small one). The effective tier uses the SAME fallback
    # the placement loop below uses (tier_of → _default_tier when not carved),
    # so the count matches what placement will actually demand.
    demand: Dict[tuple, int] = {}
    if explicit_prefix is None:
        for s in servers:
            if _is_managed_target(s, match_by_sid):
                continue        # managed/PaaS — no tier-subnet IP, don't size for it
            hk = s.identity_key or f"id:{s.id}"
            a = arch_overrides.get(hk) or archetype_for(s, None, classifier)
            if a == LZ_PENDING:
                continue        # undetermined — no VPC, no subnet to size for
            t = tier_of(s.role, tier_map)
            if t not in _tier_set:
                t = _default_tier
            demand[(a, t)] = demand.get((a, t), 0) + 1

    # --- carve one VPC per archetype (with its account + subnets) ---
    vpcs: List[Dict[str, Any]] = []
    accounts: Dict[str, str] = {}
    subnet_index: Dict[str, Dict[str, Any]] = {}   # (arch) -> {subnet_name -> subnet dict}
    capacity_errs: List[str] = []   # proactive demand-vs-capacity checks (Layer-1 fix)
    for arch, bp in archs.items():
        accounts[arch] = _account_for(arch, policy)
        vpc_cidr = ((bp.get("vpc") or {}).get("cidr")) if isinstance(bp, dict) else None
        if not vpc_cidr:
            vpcs.append({"archetype": arch, "account": accounts[arch],
                         "name": "", "cidr": "", "subnets": [],
                         "error": "no vpc.cidr in the LZ design"})
            subnet_index[arch] = {}
            continue
        vpc_name = ((bp.get("vpc") or {}).get("name")) or f"vpc-{arch}"
        # carve a subnet for every tier in tier_order (stable layout regardless
        # of which tiers currently have servers — an empty tier just shows 0).
        # Auto (no explicit prefix) → size each tier to its demand; explicit
        # prefix → equal-sized /prefix blocks (the operator's choice).
        if explicit_prefix is None:
            tier_pfxs = [(t, _prefix_for_count(demand.get((arch, t), 0)))
                         for t in tier_order]
            subnets = carve_subnets_sized(vpc_cidr, tier_pfxs)
        else:
            subnets = carve_subnets(vpc_cidr, tier_order, int(explicit_prefix))
        vpcs.append({"archetype": arch, "account": accounts[arch],
                     "name": vpc_name, "cidr": vpc_cidr, "subnets": subnets})
        # proactive capacity check: every tier with demand must have a carved
        # subnet whose usable count holds it (with the headroom already baked
        # into _prefix_for_count on the auto path). A shortfall here means the
        # VPC itself is too small — the fix is Layer 1 (widen the VPC cidr in
        # the LZ design) or a lower subnet_prefix, NOT a Layer-2 carving tweak.
        sn_by_name = {s["name"]: s for s in subnets}
        for t in tier_order:
            d = demand.get((arch, t), 0) if explicit_prefix is None else 0
            if d <= 0:
                continue
            sn = sn_by_name.get(t)
            cap = (sn or {}).get("usable", 0)
            if sn is None or cap < d:
                capacity_errs.append(
                    f"{arch}/{t}: {d} server(s) need IPs but the subnet holds "
                    f"{cap} — lower subnet_prefix (larger subnets) or widen the "
                    f"VPC cidr for {arch!r} in the LZ design (Layer 1)")
        # mutable per-subnet state (used cursor + assigned set) keyed by name
        subnet_index[arch] = {}
        for sn in subnets:
            subnet_index[arch][sn["name"]] = {
                "sn": ipaddress.ip_network(sn["cidr"], strict=False),
                "hosts": list(ipaddress.ip_network(sn["cidr"], strict=False).hosts()),
                "cursor": 1,        # 0 = gateway (reserved), assign from 1
            }

    # --- place every server (stable order by hostname) ---
    placements: Dict[str, Dict[str, Any]] = {}
    overflow: List[str] = []
    for s in sorted(servers, key=lambda x: x.identity_key or f"id:{x.id}"):
        # stable placement key: hostname (survives rebuild). Servers with no
        # hostname/fqdn would all share identity_key="" and clobber each other in
        # the placements dict — fall back to id:<server_id> so each gets its own
        # placement (operator can't pin them by hostname, but they're still placed).
        host_key = s.identity_key or f"id:{s.id}"
        # VPC = archetype (1:1). An archetype override moves the server to a
        # different VPC for placement purposes (operator pin); without one the
        # LZ classifier decides. Stale subnet pins from the OLD VPC are dropped
        # below (subnet names differ across VPCs) so the server lands in its
        # tier subnet in the new VPC.
        arch = arch_overrides.get(host_key) or archetype_for(s, None, classifier)
        arch_pin = host_key in arch_overrides and bool(arch_overrides[host_key])
        vpc = next((v for v in vpcs if v["archetype"] == arch), None)
        # undetermined host (no classifier rule matched, no operator pin): left
        # pending — no VPC/account/subnet/IP. The operator classifies it (via a
        # tag / app_map / role_map rule or a VPC pin) before it can be placed. Do
        # NOT dump it into a real VPC (which used to be the corp hub catch-all).
        if arch == LZ_PENDING and not arch_pin:
            placements[host_key] = {
                "archetype": LZ_PENDING,
                "account": "",
                "vpc": "",
                "subnet": "",
                "ip": "",
                "tier": "",
                "role": s.role or "",
                "overridden": False,
                "arch_overridden": False,
                "account_overridden": False,
                "managed": False,
                "pending": True,
            }
            continue
        tier = tier_of(s.role, tier_map)
        # a role mapped to a tier that isn't carved in this VPC (custom tier
        # names, or a tier dropped for VPC capacity) lands in the default tier
        # so it always gets a real subnet + IP instead of going unplaced.
        if tier not in subnet_index.get(arch, {}):
            tier = _default_tier
        vpc_account = (vpc or {}).get("account", "")
        acct_pin = host_key in acct_overrides and bool(acct_overrides[host_key])
        # managed/PaaS target (TencentDB / TData / COS / CFS): belongs to the
        # account/VPC but has NO per-host tier-subnet IP — record it under its
        # tier/VPC with ip="" + managed=True and consume no subnet cursor (so
        # the CVM/BM-sized subnets are not inflated by PaaS hosts).
        managed = _is_managed_target(s, match_by_sid)
        if managed:
            placements[host_key] = {
                "archetype": arch,
                "account": acct_overrides.get(host_key) or vpc_account,
                "vpc": (vpc or {}).get("name", ""),
                "subnet": "",
                "ip": "",
                "tier": tier,
                "role": s.role or "",
                "overridden": False,
                "arch_overridden": arch_pin,
                "account_overridden": acct_pin,
                "managed": True,
            }
            continue
        pinned = overrides.get(host_key)
        # a subnet pin set in the server's OLD VPC is stale after a VPC move
        # (subnet names differ across VPCs) — drop it and fall back to the
        # tier subnet in the new VPC. (The placement endpoint also clears the
        # stale pin on a VPC change; this is the defensive path for a raw
        # build with a mismatched overrides/archetype_overrides pair.)
        if pinned and pinned not in subnet_index.get(arch, {}):
            pinned = None
        overridden = bool(pinned)
        subnet_name = pinned or tier
        sn_state = subnet_index.get(arch, {}).get(subnet_name)
        ip = ""
        if vpc and sn_state:
            hosts = sn_state["hosts"]
            # reserve hosts[0] as the gateway; assign from cursor upward
            if sn_state["cursor"] < len(hosts):
                ip = str(hosts[sn_state["cursor"]])
                sn_state["cursor"] += 1
            else:
                overflow.append(f"{host_key} ({arch}/{subnet_name})")
        placements[host_key] = {
            "archetype": arch,
            "account": acct_overrides.get(host_key) or vpc_account,
            "vpc": (vpc or {}).get("name", ""),
            "subnet": subnet_name,
            "ip": ip,
            "tier": tier,
            "role": s.role or "",
            "overridden": overridden,
            "arch_overridden": arch_pin,
            "account_overridden": acct_pin,
            "managed": False,
        }
    # --- update used counts + mark overflow subnets ---
    for vpc in vpcs:
        arch = vpc["archetype"]
        used_by_sn: Dict[str, int] = {}
        for p in placements.values():
            if p["archetype"] == arch and p["ip"]:
                used_by_sn[p["subnet"]] = used_by_sn.get(p["subnet"], 0) + 1
        for sn in vpc.get("subnets", []):
            sn["used"] = used_by_sn.get(sn["name"], 0)

    nd = NetworkDesign(
        design_id=design_id, name=name or "network design",
        # the AI description = the policy's ``notes`` (MigraQ authors it when it
        # proposes the carving; empty for the deterministic default policy).
        summary=str((policy or {}).get("notes") or "").strip()[:500],
        accounts=accounts, vpcs=vpcs, placements=placements,
        policy=policy, overrides=dict(overrides), onprem_cidrs=onprem,
        archetype_overrides=dict(arch_overrides),
        account_overrides=dict(acct_overrides), is_active=False,
    )
    nd.validation = validate_network_design(nd, servers)
    if capacity_errs:
        nd.validation["ok"] = False
        nd.validation["errors"] = list(nd.validation.get("errors", [])) + capacity_errs
    if overflow:
        nd.validation["ok"] = False
        nd.validation["errors"] = list(nd.validation.get("errors", [])) + [
            f"subnet overflow (capacity exceeded): {', '.join(overflow)} — "
            "lower subnet_prefix (larger subnets) or widen the VPC cidr in the LZ design"
        ]
    return nd


# ---------------------------------------------------------------------------
# validate — subnets within VPC, non-overlap, IP in subnet, capacity
# ---------------------------------------------------------------------------
def validate_network_design(nd: NetworkDesign,
                             servers: Optional[Sequence[Server]] = None
                             ) -> Dict[str, Any]:
    """Deterministic validation of a network design. Pure over the design (+ the
    server set, for the unplaced count). Returns ``{ok, errors[]}``."""
    errs: List[str] = []
    vpcs = nd.vpcs or []
    # subnet within its VPC + non-overlap across all subnets + onprem
    all_nets: Dict[str, ipaddress._BaseNetwork] = {}
    for vpc in vpcs:
        arch = vpc.get("archetype", "")
        vnet = _vpc_net(vpc.get("cidr", ""))
        if vpc.get("cidr") and vnet is None:
            errs.append(f"{arch}: invalid vpc.cidr {vpc.get('cidr')!r}")
        for sn in vpc.get("subnets", []):
            label = f"{arch}/{sn.get('name')}"
            snet = _vpc_net(sn.get("cidr", ""))
            if snet is None:
                errs.append(f"{label}: invalid subnet.cidr {sn.get('cidr')!r}")
                continue
            all_nets[label] = snet
            if vnet is not None and not snet.subnet_of(vnet):
                errs.append(f"{label}: subnet {snet} not within VPC {vnet}")
            if sn.get("used", 0) > sn.get("usable", sn.get("capacity", 0)):
                errs.append(f"{label}: used {sn.get('used')} exceeds capacity "
                            f"{sn.get('usable', sn.get('capacity', 0))}")
    labels = list(all_nets.keys())
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            if all_nets[labels[i]].overlaps(all_nets[labels[j]]):
                errs.append(f"subnet overlap: {labels[i]} overlaps {labels[j]}")
    for oc in (nd.onprem_cidrs or []):
        onet = _vpc_net(oc)
        if onet is None:
            continue
        for label, snet in all_nets.items():
            if snet.overlaps(onet):
                errs.append(f"subnet {label} overlaps on-prem cidr {oc}")
    # placements reference valid subnets + IP within subnet
    vpc_by_arch = {v.get("archetype"): v for v in vpcs}
    for hk, p in (nd.placements or {}).items():
        arch = p.get("archetype", "")
        # undetermined hosts are intentionally unplaced (no VPC) — not an error.
        if arch == LZ_PENDING:
            continue
        vpc = vpc_by_arch.get(arch)
        if not vpc:
            errs.append(f"{hk}: archetype {arch!r} has no VPC")
            continue
        sn_names = {sn.get("name") for sn in vpc.get("subnets", [])}
        if p.get("subnet") not in sn_names and p.get("subnet"):
            errs.append(f"{hk}: subnet {p.get('subnet')!r} not in {arch} VPC")
        if p.get("ip"):
            ip = None
            try:
                ip = ipaddress.ip_address(p.get("ip"))
            except ValueError:
                errs.append(f"{hk}: invalid ip {p.get('ip')!r}")
                continue
            sn = next((s for s in vpc.get("subnets", [])
                       if s.get("name") == p.get("subnet")), None)
            if sn:
                snet = _vpc_net(sn.get("cidr", ""))
                if snet and ip not in snet:
                    errs.append(f"{hk}: ip {ip} not in subnet {snet}")
    # unplaced servers (only when the server set is provided). Use the SAME
    # host_key the builder uses (identity_key OR ``id:<id>`` fallback) — the old
    # check keyed on identity_key alone, so a hostname-less server (placed under
    # ``id:<id>``) was falsely reported as unplaced.
    if servers is not None:
        placed = set(nd.placements or {})
        unplaced: List[str] = []
        for s in servers:
            hk = s.identity_key or f"id:{s.id}"
            if hk not in placed:
                unplaced.append(hk)
        if unplaced:
            errs.append(f"{len(unplaced)} server(s) unplaced (e.g. "
                        f"{', '.join(unplaced[:5])})")
    return {"ok": not errs, "errors": errs}


__all__ = [
    "TIER_WEB", "TIER_APP", "TIER_DATA", "TIER_INFRA", "TIER_ORDER",
    "MIN_TIER_PREFIX", "DEFAULT_HEADROOM",
    "tier_of", "default_network_policy", "carve_subnets", "carve_subnets_sized",
    "_prefix_for_count", "build_network_design", "validate_network_design",
]