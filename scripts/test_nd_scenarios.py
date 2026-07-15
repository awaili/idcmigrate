"""Diagnostic: run every Network-designer starter scenario (_ND_SCENARIOS) through
the REAL LLM (LLMClient.design_network_policy) + the deterministic
build_network_design, and check the policy + carved subnets + placements meet
per-scenario expectations. Not a pytest — hits the live LLM; run directly:

    PYTHONPATH=. python scripts/test_nd_scenarios.py [--workers N] [--only "a,b"]

Uses a synthetic LZ design with corp/online/data/dmz archetypes (each a /16 VPC)
+ a role_map classifier that spreads servers, so the Rename/Merge scenarios have
valid archetype keys. Verifies: policy validates, tier_order/subnet_prefix/
account_map honored, carved subnets == tier_order per VPC, every server placed.
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from idc.config import get_settings
from idc.core.lz import archetype_for
from idc.core.models import LZDesign, Server
from idc.core.network import build_network_design, tier_of, _TIER_MAP
from idc.llm.client import LLMClient


def _arch(name, cidr):
    return {"archetype": name, "summary": name,
            "vpc": {"name": f"vpc-{name}", "cidr": cidr, "description": name},
            "peering": {}, "internet_egress": {"nat": False, "public_clb": False},
            "security_groups": [f"sg-{name}"], "cam_roles": [f"r-{name}"],
            "tag_policy": {"env": "required"}, "policy_as_code": ["require env tag"]}


def _synthetic_lz():
    archs = {"corp": _arch("corp", "10.10.0.0/16"),
             "online": _arch("online", "10.20.0.0/16"),
             "data": _arch("data", "10.30.0.0/16"),
             "dmz": _arch("dmz", "10.40.0.0/16")}
    clf = {"role_map": {"web": "online", "frontend": "online", "db": "data",
                        "cache": "data", "hadoop": "data", "k8s": "corp",
                        "monitoring": "corp", "infra": "corp", "app": "corp",
                        "general": "corp", "api": "corp", "service": "corp"},
           "default": "corp"}
    return LZDesign(design_id="t", name="synthetic", archetypes=archs,
                    requirements={"classifier": clf}, onprem_cidrs=[])


def _servers():
    roles = ["web", "frontend", "db", "cache", "hadoop", "k8s", "monitoring",
             "app", "general", "api", "infra", "service"]
    return [Server(id=f"s{i}", hostname=f"h{i}", role=roles[i % len(roles)],
                   app_ids=[f"APP_{i % 10}"]) for i in range(60)]


# (label, prompt, expectations)
#   prefix: expected subnet_prefix (None = auto)
#   tier_set: set(tier_order) must equal this
#   tier_count: len(tier_order) must equal this
#   tiers: {role: tier} the effective tier_of must resolve to (via policy+builtin)
#   account_map: {archetype: account} that must be present
ND = [
    ("4-tier auto (default)",
     'Carve each account VPC into the four tier subnets (web, app, data, infra), auto-sized, one account per archetype. Group web/frontend->web, app/general->app, db/cache/hadoop->data, k8s/monitoring/infra->infra.',
     {"prefix": None, "tier_set": {"web", "app", "data", "infra"}}),
    ("/24 subnets",
     'Use /24 subnets for every tier (subnet_prefix 24) across all account VPCs.',
     {"prefix": 24, "tier_set": {"web", "app", "data", "infra"}}),
    ("/23 (more headroom)",
     'Use /23 subnets for every tier (subnet_prefix 23) — more headroom for estates with large DB/data hosts.',
     {"prefix": 23, "tier_set": {"web", "app", "data", "infra"}}),
    ("db+cache+hadoop -> data",
     'Group db, cache, and hadoop roles into the data tier; web/frontend into web; app/general/api into app; k8s/monitoring/infra into infra. Keep all four tiers.',
     {"tier_set": {"web", "app", "data", "infra"},
      "tiers": {"db": "data", "cache": "data", "hadoop": "data", "web": "web",
                "k8s": "infra", "app": "app"}}),
    ("Flat single tier",
     'Simple lift-and-shift: collapse to a single tier — put every role in the app tier (tier_order ["app"]), one subnet per VPC.',
     {"tier_set": {"app"}, "tier_count": 1}),
    ("Web + data only",
     'Only carve the web and data tiers (tier_order ["web","data"]); drop the app and infra tiers.',
     {"tier_set": {"web", "data"}, "tier_count": 2}),
    ("Rename accounts",
     'Name the accounts explicitly: account_map data -> "data-svc", online -> "public-apps", corp -> "hub-mgmt". Keep one account per archetype.',
     {"account_map": {"data": "data-svc", "online": "public-apps", "corp": "hub-mgmt"},
      "tier_set": {"web", "app", "data", "infra"}}),
    ("Merge data + online",
     'Merge the data and online archetypes into one shared account named "workloads" (account_map data -> "workloads", online -> "workloads"); keep corp as its own account "hub-mgmt".',
     {"account_map": {"data": "workloads", "online": "workloads", "corp": "hub-mgmt"},
      "tier_set": {"web", "app", "data", "infra"}}),
    ("3 tiers public/private/protected",
     'Carve 3 tier subnets instead of the default 4: tier_order ["public","private","protected"], default_tier "private". Group web/frontend -> public; app/general/api -> private; db/cache/hadoop/k8s/monitoring/infra -> protected. One account per archetype.',
     {"tier_set": {"public", "private", "protected"}, "tier_count": 3}),
    ("Web + data only (2 tiers)",
     'Carve only 2 tier subnets: tier_order ["web","data"], default_tier "web". Group web/frontend/app/general/api -> web; db/cache/hadoop/k8s/monitoring/infra -> data. Drop the app and infra tiers.',
     {"tier_set": {"web", "data"}, "tier_count": 2}),
]


def check(label, exp, pres, nd):
    fails = []
    if not pres["ok"]:
        fails.append(f"policy not ok: {pres.get('error')} rounds={pres.get('rounds')}")
        return fails
    pol = pres["policy"]
    # prefix
    if "prefix" in exp:
        if pol.get("subnet_prefix") != exp["prefix"]:
            fails.append(f"subnet_prefix={pol.get('subnet_prefix')!r} expected {exp['prefix']!r}")
    # tier_order set / count
    to = pol.get("tier_order") or []
    if "tier_set" in exp and set(to) != exp["tier_set"]:
        fails.append(f"tier_order set={set(to)} expected {exp['tier_set']}")
    if "tier_count" in exp and len(to) != exp["tier_count"]:
        fails.append(f"tier_order count={len(to)} expected {exp['tier_count']} ({to})")
    # effective role->tier (policy tiers merged over builtin by build)
    pol_tiers = pol.get("tiers") or {}
    eff = dict(_TIER_MAP)
    for k, v in pol_tiers.items():
        if isinstance(v, str):
            eff[str(k).lower().strip()] = v
    for role, tier in (exp.get("tiers") or {}).items():
        got = tier_of(role, eff)
        if got != tier:
            fails.append(f"tier_of({role!r})={got!r} expected {tier!r}")
    # account_map
    am = pol.get("account_map") or {}
    for arch, acct in (exp.get("account_map") or {}).items():
        if am.get(arch) != acct:
            fails.append(f"account_map[{arch}]={am.get(arch)!r} expected {acct!r}")
    # build_network_design: validates + carved subnets == tier_order + all placed
    if nd is None:
        fails.append("build_network_design returned None")
        return fails
    v = nd.validation or {}
    if not v.get("ok"):
        fails.append(f"build validation failed: {v.get('errors')}")
    # carved subnets in every VPC that has a cidr == tier_order
    for vpc in (nd.vpcs or []):
        if not vpc.get("cidr"):
            continue
        names = [s["name"] for s in (vpc.get("subnets") or [])]
        if names != list(to):
            fails.append(f"VPC {vpc['archetype']}: carved {names} != tier_order {list(to)}")
            break
    # account_map honored in the built VPC account labels
    for arch, acct in (exp.get("account_map") or {}).items():
        vpc = next((v for v in (nd.vpcs or []) if v["archetype"] == arch), None)
        if vpc and vpc.get("account") != acct:
            fails.append(f"built VPC {arch}.account={vpc.get('account')!r} expected {acct!r}")
    # every server placed with an IP (default_tier fallback should cover all)
    unplaced = [h for h, p in (nd.placements or {}).items() if not p.get("ip")]
    if unplaced:
        fails.append(f"{len(unplaced)} server(s) unplaced (no IP): {unplaced[:5]}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--only", type=str, default="")
    args = ap.parse_args()
    settings = get_settings()
    lz = _synthetic_lz()
    servers = _servers()
    print(f"synthetic LZ archs={list(lz.archetypes)}; {len(servers)} servers; "
          f"llm={settings.llm_base} model={settings.llm_model}")
    combos = ND
    if args.only:
        subs = [s.strip() for s in args.only.split(",") if s.strip()]
        combos = [c for c in ND if any(s in c[0] for s in subs)]
    print(f"running {len(combos)} network scenarios with {args.workers} workers\n")

    def run_one(i, combo):
        label, prompt, exp = combo
        llm = LLMClient(get_settings())
        t = time.time()
        try:
            pres = llm.design_network_policy(prompt, servers, lz, max_rounds=2)
            pol = pres.get("policy") if pres.get("ok") else None
            nd = build_network_design(lz, servers, policy=pol) if pol else None
        except Exception as e:
            pres = {"ok": False, "error": repr(e), "rounds": [], "policy": None}
            nd = None
        dt = time.time() - t
        fails = check(label, exp, pres, nd)
        return (i, label, dt, fails, pres, nd)

    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(run_one, i, c) for i, c in enumerate(combos)]
        for f in as_completed(futs):
            i, label, dt, fails, pres, nd = f.result()
            results[i] = (label, dt, fails, pres, nd)
            mark = "✓" if not fails else "✗"
            print(f"{mark} {label:32} {dt:5.1f}s  {'OK' if not fails else 'FAIL'}")

    print("\n=== DETAILS (failures) ===")
    npass = 0
    for i in range(len(combos)):
        label, dt, fails, pres, nd = results[i]
        if not fails:
            npass += 1
            continue
        pol = pres.get("policy") or {}
        print(f"\n✗ {label}  ({dt:.1f}s, ok={pres.get('ok')})")
        for f in fails:
            print(f"   - {f}")
        print(f"   policy: tier_order={pol.get('tier_order')} prefix={pol.get('subnet_prefix')} "
              f"default_tier={pol.get('default_tier')} account_map={pol.get('account_map')} "
              f"tiers={dict(list((pol.get('tiers') or {}).items())[:6])}")
        if pres.get("rounds"):
            print(f"   round errs: {pres['rounds'][-1].get('errors')}")

    print(f"\n=== {npass}/{len(combos)} network scenarios passed ===")
    sys.exit(0 if npass == len(combos) else 1)


if __name__ == "__main__":
    main()