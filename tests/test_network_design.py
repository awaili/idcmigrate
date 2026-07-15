"""F8 (Phase B+) — the network designer: VPC + subnets per account + placement.

Covers the deterministic engine (``idc.core.network``), the optional LLM policy
proposer (``LLMClient.design_network_policy``, mocked), the store layer
(``network_designs`` table), and the backend endpoints. The LLM path is mocked
(``patch.object(c, "chat", ...)``) — no real gateway is hit.
"""
import json
from unittest.mock import patch

import pytest

from idc.core.lz import default_lz_design, LZ_PENDING
from idc.core.models import Server, NetworkDesign
from idc.core.network import (
    TIER_ORDER, build_network_design, carve_subnets, default_network_policy,
    tier_of, validate_network_design,
)


def _srv(role="", tags=None, hostname="h", sid="s"):
    return Server(id=sid, hostname=hostname, role=role, tags=tags or [])


# ---------------------------------------------------------------------------
# tier_of
# ---------------------------------------------------------------------------
def test_tier_of_maps_roles_to_four_tiers():
    assert tier_of("web") == "web"
    assert tier_of("frontend") == "web"
    assert tier_of("db") == "data"
    assert tier_of("cache") == "data"
    assert tier_of("k8s") == "infra"
    assert tier_of("monitoring") == "infra"
    assert tier_of("app") == "app"
    assert tier_of("general") == "app"
    assert tier_of("whatever-unknown") == "app"     # default tier
    assert set(TIER_ORDER) == {"web", "app", "data", "infra"}


# ---------------------------------------------------------------------------
# carve_subnets
# ---------------------------------------------------------------------------
def test_carve_subnets_within_vpc_non_overlapping_with_gateway():
    sn = carve_subnets("10.10.0.0/16", ["web", "app", "data", "infra"])
    assert len(sn) == 4
    cidrs = [s["cidr"] for s in sn]
    # all within the /16 and pairwise non-overlapping (sequential carve)
    import ipaddress
    nets = [ipaddress.ip_network(c) for c in cidrs]
    vpc = ipaddress.ip_network("10.10.0.0/16")
    for n in nets:
        assert n.subnet_of(vpc)
    for i in range(len(nets)):
        for j in range(i + 1, len(nets)):
            assert not nets[i].overlaps(nets[j])
    # gateway is the first host; capacity excludes network+broadcast
    assert sn[0]["gateway"] == "10.10.0.1"
    assert sn[0]["capacity"] == 2 ** (32 - 18) - 2     # /18


def test_carve_subnets_auto_prefix_yields_four():
    # no explicit prefix -> vpc_prefix+2 -> 4 subnets
    sn = carve_subnets("10.20.0.0/16", TIER_ORDER)
    assert len(sn) == 4
    assert [s["name"] for s in sn] == list(TIER_ORDER)


def test_carve_subnets_drops_tiers_that_dont_fit():
    # a /30 vpc can hold at most 4 /32s but with auto-prefix = /32 -> 0 usable;
    # still returns one subnet per tier up to what fits
    sn = carve_subnets("10.0.0.0/30", ["a", "b", "c", "d", "e"])
    assert len(sn) <= 5     # bounded by what fits in the vpc


# ---------------------------------------------------------------------------
# build_network_design — placement + IP assignment
# ---------------------------------------------------------------------------
def test_build_places_servers_into_tier_subnets_with_sequential_ips():
    lz = default_lz_design()
    servers = [
        _srv(role="web", hostname="w1", sid="a"),
        _srv(role="web", hostname="w2", sid="b"),
        _srv(role="db", hostname="d1", sid="c"),
        _srv(role="app", hostname="a1", sid="d"),
    ]
    nd = build_network_design(lz, servers)
    assert nd.validation["ok"], nd.validation["errors"]
    # web -> online VPC web subnet; db/app -> corp (default archetype)
    assert nd.placements["w1"]["subnet"] == "web"
    assert nd.placements["w1"]["vpc"] == "vpc-online"
    assert nd.placements["w1"]["ip"] == "10.20.0.2"     # gateway .1 reserved
    assert nd.placements["w2"]["ip"] == "10.20.0.3"     # sequential
    assert nd.placements["d1"]["subnet"] == "data"
    assert nd.placements["d1"]["vpc"] == "vpc-corp"
    # every archetype mapped to an account — account name IS the archetype name
    # (account naming is Layer 1; the network designer doesn't synthesize acct-<arch>)
    assert nd.accounts["corp"] == "corp"
    assert nd.accounts["online"] == "online"


def test_build_is_deterministic_and_stable_order():
    lz = default_lz_design()
    servers = [_srv(role="app", hostname=f"h{i}", sid=f"s{i}") for i in range(6)]
    nd1 = build_network_design(lz, servers)
    nd2 = build_network_design(lz, list(reversed(servers)))   # same set, diff order
    # placements keyed by hostname -> identical regardless of input order
    assert nd1.placements == nd2.placements


def test_build_reports_overflow_when_subnet_too_small():
    lz = default_lz_design()
    # force tiny subnets (/30 -> ~2 usable) with many servers in one tier
    pol = {"subnet_prefix": 30, "tier_order": ["app"], "tiers": {}, "account_map": {}}
    servers = [_srv(role="app", hostname=f"h{i}", sid=f"s{i}") for i in range(10)]
    nd = build_network_design(lz, servers, policy=pol)
    assert nd.validation["ok"] is False
    assert any("overflow" in e for e in nd.validation["errors"])
    # only the first few fit (gateway + 1 usable on /30 -> ~1 host assignable)
    placed = sum(1 for p in nd.placements.values() if p["ip"])
    assert placed < 10


def test_build_override_moves_server_to_a_different_subnet_and_flags_it():
    lz = default_lz_design()
    servers = [_srv(role="db", hostname="db1", sid="c")]   # db -> corp/data
    nd = build_network_design(lz, servers, overrides={"db1": "web"})
    p = nd.placements["db1"]
    assert p["subnet"] == "web"
    assert p["overridden"] is True
    assert p["archetype"] == "corp"      # still corp VPC, just web subnet
    assert nd.overrides == {"db1": "web"}


def test_build_override_survives_regenerate():
    """Rebuilding with the stored overrides reapplies the pin (the override is
    the source of truth, not the first placement)."""
    lz = default_lz_design()
    servers = [_srv(role="db", hostname="db1", sid="c"),
               _srv(role="app", hostname="a1", sid="d")]
    nd = build_network_design(lz, servers, overrides={"db1": "web"})
    # regenerate from the stored overrides
    nd2 = build_network_design(lz, servers, policy=nd.policy, overrides=nd.overrides)
    assert nd2.placements["db1"]["subnet"] == "web"
    assert nd2.placements["db1"]["overridden"] is True


def test_build_hostname_less_servers_dont_clobber():
    """Servers with no hostname/fqdn share identity_key="" — they must NOT
    overwrite each other in the placements dict (fall back to id:<server_id>)."""
    lz = default_lz_design()
    servers = [Server(id="s1", hostname="", role="app"),
              Server(id="s2", hostname="", role="db"),
              Server(id="s3", hostname="", role="web")]
    nd = build_network_design(lz, servers)
    # all three placed, none clobbered (3 distinct keys)
    assert len(nd.placements) == 3
    assert "id:s1" in nd.placements and "id:s2" in nd.placements and "id:s3" in nd.placements
    # each got an IP
    assert all(p["ip"] for p in nd.placements.values())


def test_build_undetermined_host_is_pending_not_dumped_into_corp():
    """A host the classifier can't place (no signal under the built-in default;
    or no tag/app/role rule matched under a custom design) is left PENDING — no
    VPC, no account, no subnet, no IP — NOT dumped into the corp hub account, and
    validate_network_design does NOT flag it as an error."""
    # built-in default: a signal-less host (no role, no tags) -> pending
    lz = default_lz_design()
    blank = Server(id="s0", hostname="blank1", role="", tags=[])
    placed = Server(id="s1", hostname="app1", role="app")
    nd = build_network_design(lz, [blank, placed])
    p0 = nd.placements["blank1"]
    assert p0["archetype"] == LZ_PENDING
    assert p0["vpc"] == "" and p0["subnet"] == "" and p0["ip"] == ""
    assert p0["pending"] is True
    # the role'd host is still placed normally (corp hub, real IP)
    assert nd.placements["app1"]["archetype"] == "corp"
    assert nd.placements["app1"]["ip"]
    # the pending host is NOT an error (it is intentionally unplaced)
    assert nd.validation["ok"] is True, nd.validation["errors"]

    # custom design whose rules don't cover a role -> that host is pending too
    lz2 = LZDesign(design_id="t2", name="t2",
                   archetypes={"hub": {"vpc": {"name": "vpc-hub",
                                               "cidr": "10.10.0.0/16"}}},
                   requirements={"classifier": {"role_map": {"web": "hub"}}})
    nd2 = build_network_design(lz2, [Server(id="s2", hostname="x1", role="cache")])
    assert nd2.placements["x1"]["archetype"] == LZ_PENDING
    assert nd2.placements["x1"]["pending"] is True
    assert nd2.validation["ok"] is True, nd2.validation["errors"]


# ---------------------------------------------------------------------------
# demand-aware auto carving + proactive IP-exhaustion prevention
# ---------------------------------------------------------------------------
from idc.core.network import _prefix_for_count, carve_subnets_sized  # noqa: E402
from idc.core.models import LZDesign, Match, Target  # noqa: E402


def test_prefix_for_count_sizes_to_demand_and_floors_at_28():
    # empty / tiny tiers floor at /28 (14 usable) — never a /30 with 1 host
    assert _prefix_for_count(0) == 28
    assert _prefix_for_count(2) == 28
    # bigger demand -> bigger block (smaller prefix), monotonically
    assert _prefix_for_count(300) == 22      # ~600 with headroom -> /22 (1022)
    assert _prefix_for_count(6000) == 18     # ~12000 with headroom -> /18 (16382)
    assert _prefix_for_count(300) > _prefix_for_count(6000)   # 22 > 18 (smaller block)


def test_prefix_for_count_holds_demand_and_is_monotonic():
    """Every sized prefix must actually hold its count (with headroom + reserved),
    and the prefix is non-increasing as demand grows (bigger count -> bigger block)."""
    prev = 32
    for c in [0, 1, 2, 5, 13, 14, 15, 100, 300, 1000, 6000, 16000, 50000]:
        p = _prefix_for_count(c)
        assert p <= 28                                 # never tighter than the /28 floor
        usable = max(0, 2 ** (32 - p) - 3)             # minus net + bcast + gateway
        assert usable >= c, f"count {c}: /{p} holds {usable} < {c}"
        assert p <= prev, f"monotonic broke at {c}: {p} > {prev}"
        prev = p


def test_carve_subnets_sized_drops_tiers_that_dont_fit():
    """A /28 VPC holds exactly one /28 — extra tiers are dropped (caller validates)."""
    sn = carve_subnets_sized("10.0.0.0/28", [("a", 28), ("b", 28), ("c", 28)])
    assert [s["name"] for s in sn] == ["a"]            # only the first fits
    # a tier needing a block bigger than the VPC is dropped (prefix < vpc prefix)
    sn = carve_subnets_sized("10.0.0.0/28", [("big", 24), ("small", 28)])
    assert [s["name"] for s in sn] == ["small"]        # /24 can't fit in a /28


def test_auto_carve_sizes_each_tier_subnet_to_its_demand():
    """Lopsided estate: 300 db hosts in corp/data, a handful of web. Auto carve
    gives the busy data tier a LARGE subnet and the empty web tier a tiny one
    (instead of equal blocks), and every host still gets an IP — no overflow."""
    lz = default_lz_design()
    servers = ([_srv(role="db", hostname=f"db{i}", sid=f"d{i}") for i in range(300)]
               + [_srv(role="web", hostname="w1", sid="w1")])
    nd = build_network_design(lz, servers)           # auto prefix (None)
    assert nd.validation["ok"], nd.validation["errors"]
    corp = next(v for v in nd.vpcs if v["archetype"] == "corp")
    data_sn = next(s for s in corp["subnets"] if s["name"] == "data")
    web_sn = next(s for s in corp["subnets"] if s["name"] == "web")
    # data tier (300 hosts) gets a bigger block than the empty web tier
    data_pfx = int(data_sn["cidr"].split("/")[1])
    web_pfx = int(web_sn["cidr"].split("/")[1])
    assert data_pfx < web_pfx            # smaller prefix = larger subnet
    assert data_sn["usable"] >= 300      # holds the demand (with headroom)
    # every db host got a real IP (no exhaustion / overflow)
    db_ips = [p["ip"] for h, p in nd.placements.items() if h.startswith("db")]
    assert len(db_ips) == 300 and all(db_ips)


def test_auto_carve_proactive_error_when_vpc_too_small_for_demand():
    """A /28 VPC (14 usable) can't hold 30 app hosts in one tier. The engine
    must FAIL LOUDLY up front — a proactive capacity error naming the tier/VPC
    and the Layer-1 fix (widen the VPC cidr) — not silently drop 30 hosts."""
    lz = LZDesign(design_id="t", name="t",
                 archetypes={"tiny": {"vpc": {"name": "vpc-tiny",
                                              "cidr": "10.99.0.0/28"}}},
                 requirements={"classifier": {"role_map": {"app": "tiny"}}})
    servers = [_srv(role="app", hostname=f"h{i}", sid=f"s{i}") for i in range(30)]
    nd = build_network_design(lz, servers)
    assert nd.validation["ok"] is False
    errs = nd.validation["errors"]
    assert any("tiny/app" in e and "widen the VPC cidr" in e for e in errs), errs


def test_carve_subnets_sized_is_non_overlapping_and_within_vpc():
    sn = carve_subnets_sized("10.10.0.0/16",
                             [("data", 18), ("web", 22), ("app", 28), ("infra", 28)])
    import ipaddress
    vpc = ipaddress.ip_network("10.10.0.0/16")
    nets = [ipaddress.ip_network(s["cidr"]) for s in sn]
    for n in nets:
        assert n.subnet_of(vpc)
    for i in range(len(nets)):
        for j in range(i + 1, len(nets)):
            assert not nets[i].overlaps(nets[j])
    # returned in the caller's tier order, largest block first in address space
    assert [s["name"] for s in sn] == ["data", "web", "app", "infra"]
    assert sn[0]["cidr"] == "10.10.0.0/18"      # largest block at offset 0


def test_paas_managed_targets_excluded_from_sizing_and_get_no_ip():
    """A host matched to a managed/PaaS product (CDB = TencentDB) becomes a
    managed instance — no per-host tier-subnet IP. It is EXCLUDED from the
    per-tier demand (so the CIDR is not inflated by PaaS) and placed with
    ip="" + managed=True, while CVM hosts still count + get a real IP."""
    lz = default_lz_design()
    # 50 db hosts -> TencentDB (managed): no tier IP, excluded from demand
    managed = [Server(id=f"m{i}", hostname=f"mdb{i}", role="db") for i in range(50)]
    matches = [Match(server_id=s.id, target=Target(product="CDB")) for s in managed]
    # 5 app hosts -> CVM (no match): real tier IP, counted
    cvm = [_srv(role="app", hostname=f"a{i}", sid=f"cv{i}") for i in range(5)]
    nd = build_network_design(lz, managed + cvm, matches=matches)
    assert nd.validation["ok"], nd.validation["errors"]
    # managed hosts placed under their account/VPC/tier but with no IP
    mp = nd.placements["mdb0"]
    assert mp["managed"] is True and mp["ip"] == "" and mp["subnet"] == ""
    assert mp["archetype"] == "corp"          # db -> corp
    # CVM hosts got a real IP + managed=False
    assert nd.placements["a0"]["managed"] is False and nd.placements["a0"]["ip"]
    # the corp/data subnet was sized for the CVM demand only (0 here -> /28,
    # 13 usable) — NOT for the 50 managed db hosts, proving they were excluded
    corp = next(v for v in nd.vpcs if v["archetype"] == "corp")
    data_sn = next(s for s in corp["subnets"] if s["name"] == "data")
    assert data_sn["usable"] < 50
    assert sum(1 for p in nd.placements.values() if p.get("managed")) == 50


def test_managed_product_matrix_and_compute_products_stay_placed():
    """Every MANAGED product -> managed (no IP); CVM/BM/TKE/EMR/unknown/empty ->
    NOT managed (gets an IP). Pins the PaaS-vs-compute boundary in one place."""
    from idc.core.network import MANAGED_PRODUCTS
    lz = default_lz_design()
    for prod in MANAGED_PRODUCTS:
        s = Server(id="x", hostname="xh", role="db")
        m = Match(server_id="x", target=Target(product=prod))
        nd = build_network_design(lz, [s], matches=[m])
        assert nd.placements["xh"]["managed"] is True, f"{prod} not managed"
        assert nd.placements["xh"]["ip"] == "", f"{prod} got an IP"
    for prod in ["CVM", "BM", "TKE", "EMR", "SomethingNew", ""]:
        s = Server(id="y", hostname="yh", role="app")
        m = Match(server_id="y", target=Target(product=prod))
        nd = build_network_design(lz, [s], matches=[m])
        assert nd.placements["yh"]["managed"] is False, f"{prod!r} wrongly managed"
        assert nd.placements["yh"]["ip"], f"{prod!r} got no IP"
    # no match at all -> defaults to a CVM (legacy all-placed behaviour preserved)
    nd = build_network_design(lz, [_srv(role="app", hostname="z", sid="z")])
    assert nd.placements["z"]["managed"] is False and nd.placements["z"]["ip"]


def test_mixed_paas_and_cvm_in_same_tier_sized_for_cvm_only():
    """200 managed app hosts + 40 CVM app hosts in the same tier: the subnet is
    sized for the 40 CVM (not 240), CVM hosts get IPs, managed hosts get none."""
    lz = default_lz_design()
    cvm = [_srv(role="app", hostname=f"c{i}", sid=f"c{i}") for i in range(40)]
    managed = [Server(id=f"p{i}", hostname=f"p{i}", role="app") for i in range(200)]
    matches = [Match(server_id=f"p{i}", target=Target(product="CDB"))
               for i in range(200)]
    nd = build_network_design(lz, cvm + managed, matches=matches)
    assert nd.validation["ok"], nd.validation["errors"]
    corp = next(v for v in nd.vpcs if v["archetype"] == "corp")
    app = next(s for s in corp["subnets"] if s["name"] == "app")
    assert 40 <= app["usable"] < 240            # sized for CVM, not inflated by PaaS
    assert all(nd.placements[f"c{i}"]["ip"] for i in range(40))
    assert all(nd.placements[f"p{i}"]["ip"] == "" for i in range(200))
    assert sum(1 for p in nd.placements.values() if p.get("managed")) == 200


def test_explicit_prefix_equal_blocks_and_proactive_overflow():
    """An explicit subnet_prefix carves EQUAL blocks; when demand exceeds a
    block's usable count the design fails (overflow) instead of silently dropping."""
    lz = default_lz_design()
    pol = {"subnet_prefix": 30, "tier_order": ["app"], "tiers": {}}
    nd = build_network_design(lz, [_srv(role="app", hostname=f"h{i}",
                                        sid=f"s{i}") for i in range(10)],
                              policy=pol)
    assert nd.validation["ok"] is False
    assert any("overflow" in e for e in nd.validation["errors"])
    # equal-sized blocks for an explicit prefix
    sn = carve_subnets("10.10.0.0/16", ["app", "web"], 24)
    assert all(s["cidr"].endswith("/24") for s in sn)


# ---------------------------------------------------------------------------
# validate_network_design — catches a hand-broken design
# ---------------------------------------------------------------------------
def test_validate_catches_ip_out_of_subnet_and_overlap():
    nd = NetworkDesign(
        design_id="x", name="x",
        accounts={"corp": "acct-corp"},
        vpcs=[{"archetype": "corp", "account": "acct-corp", "name": "vpc-corp",
               "cidr": "10.10.0.0/16",
               "subnets": [
                   {"name": "web", "cidr": "10.10.0.0/18", "tier": "web",
                    "capacity": 16382, "usable": 16381, "used": 0, "gateway": "10.10.0.1"},
                   {"name": "app", "cidr": "10.10.64.0/18", "tier": "app",
                    "capacity": 16382, "usable": 16381, "used": 0, "gateway": "10.10.64.1"},
               ]}],
        placements={"h1": {"archetype": "corp", "account": "acct-corp",
                          "vpc": "vpc-corp", "subnet": "web", "ip": "10.20.0.5",
                          "tier": "web", "role": "web", "overridden": False}},
        onprem_cidrs=[],
    )
    v = validate_network_design(nd)
    assert v["ok"] is False
    assert any("not in subnet" in e for e in v["errors"])   # 10.20.0.5 not in 10.10.0.0/18


def test_validate_catches_subnet_overlap_with_onprem():
    nd = NetworkDesign(
        design_id="x", name="x", accounts={"a": "acct-a"},
        vpcs=[{"archetype": "a", "account": "acct-a", "name": "vpc-a",
               "cidr": "10.10.0.0/16",
               "subnets": [{"name": "web", "cidr": "10.10.0.0/18", "tier": "web",
                            "capacity": 16382, "usable": 16381, "used": 0,
                            "gateway": "10.10.0.1"}]}],
        placements={}, onprem_cidrs=["10.10.0.0/24"],   # overlaps the web subnet
    )
    v = validate_network_design(nd)
    assert v["ok"] is False
    assert any("on-prem" in e for e in v["errors"])


# ---------------------------------------------------------------------------
# LLM design_network_policy (mocked)
# ---------------------------------------------------------------------------
from idc.config import Settings  # noqa: E402
from idc.llm.client import LLMClient  # noqa: E402


def _client():
    return LLMClient(Settings(llm_enabled=True))


def test_design_network_policy_valid_first_try():
    pol = json.dumps({"subnet_prefix": 24, "tier_order": list(TIER_ORDER),
                      "tiers": {"web": "web"}, "account_map": {}, "notes": "x"})
    c = _client()
    with patch.object(c, "chat", return_value=pol):
        r = c.design_network_policy("/24 subnets", [_srv(role="web")],
                                     default_lz_design())
    assert r["ok"] is True
    assert r["policy"]["subnet_prefix"] == 24
    assert len(r["rounds"]) == 1 and r["rounds"][0]["ok"]


def test_design_network_policy_loops_on_bad_prefix_then_fixes():
    bad = json.dumps({"subnet_prefix": 99, "tier_order": list(TIER_ORDER),
                     "account_map": {}})
    good = json.dumps({"subnet_prefix": 20, "tier_order": list(TIER_ORDER),
                       "account_map": {}})
    calls = []

    def fake(messages, stream=False, timeout=None):
        calls.append(messages)
        return bad if len(calls) == 1 else good

    c = _client()
    with patch.object(c, "chat", side_effect=fake):
        r = c.design_network_policy("bigger subnets", [_srv(role="app")],
                                     default_lz_design())
    assert r["ok"] is True
    assert r["policy"]["subnet_prefix"] == 20
    assert len(r["rounds"]) == 2
    assert any("subnet_prefix" in e for e in r["rounds"][0]["errors"])


def test_design_network_policy_disabled_returns_default():
    c = LLMClient(Settings(llm_enabled=False))
    r = c.design_network_policy("x", [], default_lz_design())
    assert r["ok"] is False
    assert r["policy"]["subnet_prefix"] is None     # default (auto)


# -- custom tier names (operator asks for public/private/protected, not the
#    default web/app/data/infra) — the validator must accept them and the engine
#    must carve + place against them. Regression for the bug where the validator
#    hard-rejected any tier_order not in TIER_ORDER, so the LLM was looped back
#    to the default 4 tiers and the operator's naming was ignored.
def test_design_network_policy_accepts_custom_tier_names():
    pol = json.dumps({"subnet_prefix": None,
                      "tier_order": ["public", "private", "protected"],
                      "default_tier": "private",
                      "tiers": {"web": "public", "frontend": "public",
                                "general": "private", "app": "private",
                                "api": "private",
                                "db": "protected", "cache": "protected",
                                "hadoop": "protected", "k8s": "protected",
                                "monitoring": "protected", "infra": "protected"},
                      "account_map": {}, "notes": "3 tiers"})
    c = _client()
    with patch.object(c, "chat", return_value=pol):
        r = c.design_network_policy("3 tiers public/private/protected",
                                    [_srv(role="web")], default_lz_design())
    assert r["ok"] is True
    assert r["policy"]["tier_order"] == ["public", "private", "protected"]
    assert r["policy"]["default_tier"] == "private"
    assert len(r["rounds"]) == 1 and r["rounds"][0]["ok"]


def test_design_network_policy_rejects_tier_value_not_in_tier_order():
    # 'secure' isn't in the proposed tier_order → rejected, so a mis-named tier
    # is caught even with custom names (tiers values must track tier_order).
    bad = json.dumps({"tier_order": ["public", "private", "protected"],
                      "tiers": {"db": "secure"}})
    c = _client()
    with patch.object(c, "chat", return_value=bad):
        r = c.design_network_policy("3 tiers", [_srv(role="db")],
                                    default_lz_design())
    assert r["ok"] is False
    assert any("tiers values must be in tier_order" in e
               for e in r["rounds"][0]["errors"])


def test_build_carves_custom_tier_subnets_and_places_every_role():
    """The engine carves one subnet per custom tier name and places every server
    (roles not mapped fall back to default_tier, so none go unplaced)."""
    lz = default_lz_design()
    servers = [_srv(role=role, hostname=f"h{i}", sid=f"s{i}")
               for i, role in enumerate(["web", "frontend", "db", "cache",
                                         "hadoop", "k8s", "monitoring", "api",
                                         "general", "infra", "service"]*3)]
    pol = {"tier_order": ["public", "private", "protected"],
           "default_tier": "private",
           "tiers": {"web": "public", "frontend": "public", "general": "private",
                     "app": "private", "api": "private", "db": "protected",
                     "cache": "protected", "hadoop": "protected",
                     "k8s": "protected", "monitoring": "protected",
                     "infra": "protected"},
           "account_map": {}}
    nd = build_network_design(lz, servers, policy=pol)
    assert [s["name"] for s in nd.vpcs[0]["subnets"]] == ["public", "private",
                                                          "protected"]
    assert nd.validation["ok"] is True
    # every server got an IP (none unplaced — 'service' isn't in tiers, falls
    # back to default_tier 'private')
    assert all(p["ip"] for p in nd.placements.values())
    # 'service' (not in the tiers map) landed in the default tier
    svc = next(p for h, p in nd.placements.items() if h.startswith("h10"))
    assert svc["subnet"] == "private"


# ---------------------------------------------------------------------------
# backend: store roundtrip + endpoints
# ---------------------------------------------------------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient  # noqa: E402
import idc.backend.app as m  # noqa: E402
from idc.core.db import open_store  # noqa: E402
from idc.config import get_settings  # noqa: E402
from idc.core.models import _new_id  # noqa: E402

client = TestClient(m.app)


def _cleanup_nd(design_ids, hostname=None):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("UPDATE network_designs SET is_active=0"))
        for did in design_ids:
            cur.execute(st._x("DELETE FROM network_designs WHERE design_id=?"), (did,))
        if hostname:
            cur.execute(st._x("DELETE FROM servers WHERE hostname=?"), (hostname,))
    st.close()


def test_store_network_design_roundtrip_and_single_active():
    st = open_store(get_settings().db_url)
    ids = []
    try:
        lz = default_lz_design()
        nd1 = build_network_design(lz, [_srv(role="app", hostname="z1", sid="z1")],
                                    design_id=_new_id("nd"))
        nd2 = build_network_design(lz, [_srv(role="web", hostname="z2", sid="z2")],
                                    design_id=_new_id("nd"))
        ids = [nd1.design_id, nd2.design_id]
        st.upsert_network_design(nd1)
        st.upsert_network_design(nd2)
        assert len(st.list_network_designs()) >= 2
        assert st.get_active_network_design() is None
        st.set_active_network_design(nd1.design_id, updated_by="test")
        assert st.get_active_network_design().design_id == nd1.design_id
        st.set_active_network_design(nd2.design_id, updated_by="test")
        assert st.get_active_network_design().design_id == nd2.design_id   # single-active
        got = st.get_network_design(nd1.design_id)
        assert got is not None and got.is_active is False
        # placements survive the JSON round-trip
        assert "z1" in got.placements
    finally:
        _cleanup_nd(ids)
        st.close()


def test_store_network_design_empty_vpcs_roundtrips_as_list_not_dict():
    """Regression: an empty vpcs list [] used to round-trip to {} (wrong type)
    because ``loads(v) or {}`` treats [] as falsy. vpcs must stay a list."""
    st = open_store(get_settings().db_url)
    did = _new_id("nd")
    try:
        nd = NetworkDesign(design_id=did, name="empty", vpcs=[], placements={},
                           onprem_cidrs=[], accounts={}, policy={}, overrides={},
                           validation={"ok": True, "errors": []})
        st.upsert_network_design(nd)
        got = st.get_network_design(did)
        assert got is not None
        assert got.vpcs == []                    # list, not {}
        assert isinstance(got.vpcs, list)
        assert got.onprem_cidrs == []            # list, not {}
    finally:
        _cleanup_nd([did])
        st.close()


def test_network_design_endpoint_falls_back_to_default_when_llm_policy_invalid():
    """Regression: when the LLM proposes an invalid carving policy (fails the
    validate loop), the endpoint must fall back to the deterministic default
    policy (valid) instead of building with the invalid last-attempted policy
    (which yields empty subnets). Mock the LLM to emit a bad prefix."""
    from unittest.mock import patch
    bad = json.dumps({"subnet_prefix": 99, "tier_order": list(TIER_ORDER),
                     "account_map": {}})   # prefix 99 > 30 -> validator rejects
    with patch.object(m.LLM, "chat", return_value=bad):
        r = client.post("/api/lz/network/design",
                        json={"demand": "garbage size", "activate": False})
    assert r.status_code == 200, r.text
    body = r.json()
    # fell back to the default policy -> a real, valid topology (not empty subnets)
    assert body["ok"] is True, body["validation"]
    assert any(v["subnets"] for v in body["design"]["vpcs"])   # subnets were carved


def test_network_design_endpoint_builds_and_activates():
    """POST /api/lz/network/design with activate builds a design over the estate
    and makes it active; GET /api/lz/network/design returns it."""
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active"] is True
    did = body["design"]["design_id"]
    try:
        # built over the active LZ design's archetypes
        arch_names = {v["archetype"] for v in body["design"]["vpcs"]}
        assert arch_names == {"corp", "online", "dmz"}      # built-in default
        # each VPC has 4 tier subnets
        for v in body["design"]["vpcs"]:
            assert {s["name"] for s in v["subnets"]} == set(TIER_ORDER)
        # active endpoint returns it
        active = client.get("/api/lz/network/design").json()
        assert active["active"] is not None
        assert active["active"]["design_id"] == did
        # listings show it active
        listed = client.get("/api/lz/network/designs").json()
        assert any(d["design_id"] == did and d["is_active"] for d in listed["designs"])
    finally:
        _cleanup_nd([did])


def test_placement_override_moves_server_and_persists():
    """Activate a design, then move a seeded server to a different subnet via the
    placement endpoint — the active design reflects the override."""
    # seed a server (role db -> corp/data by default)
    host = f"ndtest-{_new_id('h')[-6:]}"
    st = open_store(get_settings().db_url)
    st.upsert_server(_srv(role="db", hostname=host, sid=_new_id("s")))
    st.close()
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    did = r.json()["design"]["design_id"]
    try:
        before = client.get("/api/lz/network/design").json()["active"]
        assert before["placements"][host]["subnet"] == "data"   # db -> data tier
        # move it to the web subnet of the corp VPC
        pr = client.post("/api/lz/network/placement",
                         json={"hostname": host, "subnet": "web", "by": "test"})
        assert pr.status_code == 200, pr.text
        assert pr.json()["placement"]["subnet"] == "web"
        assert pr.json()["placement"]["overridden"] is True
        # the active design (reloaded) carries the override
        after = client.get("/api/lz/network/design").json()["active"]
        assert after["placements"][host]["subnet"] == "web"
        assert after["overrides"].get(host) == "web"
    finally:
        _cleanup_nd([did], hostname=host)


def test_placement_override_rejects_unknown_subnet():
    host = f"ndtest-{_new_id('h')[-6:]}"
    st = open_store(get_settings().db_url)
    st.upsert_server(_srv(role="app", hostname=host, sid=_new_id("s")))
    st.close()
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    did = r.json()["design"]["design_id"]
    try:
        pr = client.post("/api/lz/network/placement",
                         json={"hostname": host, "subnet": "nope", "by": "test"})
        assert pr.status_code == 400
    finally:
        _cleanup_nd([did], hostname=host)


def test_placement_override_requires_active_design():
    # clear any active design first
    _cleanup_nd([])
    pr = client.post("/api/lz/network/placement",
                     json={"hostname": "whatever", "subnet": "web"})
    assert pr.status_code == 409


# ---------------------------------------------------------------------------
# VPC change (archetype override) — F8 Phase B+: move a server to a different
# VPC. VPC is 1:1 with archetype, so the override reclassifies the server for
# placement without touching its inventory tags / the LZ classifier.
# ---------------------------------------------------------------------------
def test_build_archetype_override_moves_server_to_a_different_vpc():
    lz = default_lz_design()
    servers = [_srv(role="db", hostname="db1", sid="c")]   # db -> corp by default
    nd = build_network_design(lz, servers, archetype_overrides={"db1": "online"})
    p = nd.placements["db1"]
    assert p["archetype"] == "online"
    assert p["vpc"] == "vpc-online"
    assert p["account"] == "online"          # account name = archetype name
    assert p["arch_overridden"] is True
    assert p["subnet"] == "data"          # tier subnet in the new VPC
    assert nd.archetype_overrides == {"db1": "online"}
    assert nd.validation["ok"], nd.validation["errors"]


def test_build_archetype_override_keeps_valid_subnet_pin():
    """A subnet pin whose name exists in the NEW VPC (the tier names are shared
    across VPCs) is carried over — only a stale (non-existent) name is dropped."""
    lz = default_lz_design()
    servers = [_srv(role="db", hostname="db1", sid="c")]
    nd = build_network_design(lz, servers, overrides={"db1": "web"},
                              archetype_overrides={"db1": "online"})
    p = nd.placements["db1"]
    assert p["archetype"] == "online"
    assert p["vpc"] == "vpc-online"
    assert p["subnet"] == "web"           # pin valid in online VPC -> carried
    assert p["overridden"] is True
    assert p["arch_overridden"] is True


def test_build_archetype_override_drops_stale_subnet_pin():
    """A subnet pin that names a subnet NOT in the new VPC is dropped — the
    server lands in its tier subnet in the new VPC instead of breaking."""
    lz = default_lz_design()
    servers = [_srv(role="db", hostname="db1", sid="c")]
    # "legacy-tier" isn't a real subnet in any VPC — combined with a VPC move
    # the guard drops it rather than emitting a placement pointing at a
    # non-existent subnet.
    nd = build_network_design(lz, servers, overrides={"db1": "legacy-tier"},
                              archetype_overrides={"db1": "online"})
    p = nd.placements["db1"]
    assert p["archetype"] == "online"
    assert p["subnet"] == "data"         # fell back to the tier subnet
    assert p["overridden"] is False
    assert nd.validation["ok"], nd.validation["errors"]


def test_placement_override_changes_vpc_and_persists():
    """Activate a design, then move a seeded server to a different VPC via the
    placement endpoint — the active design + the Estate classification overlay
    both reflect the VPC pin."""
    host = f"ndtest-{_new_id('h')[-6:]}"
    st = open_store(get_settings().db_url)
    st.upsert_server(_srv(role="db", hostname=host, sid=_new_id("s")))
    st.close()
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    did = r.json()["design"]["design_id"]
    try:
        before = client.get("/api/lz/network/design").json()["active"]
        before_arch = before["placements"][host]["archetype"]
        # move it to the online VPC
        pr = client.post("/api/lz/network/placement",
                         json={"hostname": host, "archetype": "online", "by": "test"})
        assert pr.status_code == 200, pr.text
        place = pr.json()["placement"]
        assert place["archetype"] == "online"
        assert place["vpc"] == "vpc-online"
        assert place["arch_overridden"] is True
        assert before_arch != "online"            # actually moved
        # the active design (reloaded) carries the archetype override
        after = client.get("/api/lz/network/design").json()["active"]
        assert after["placements"][host]["archetype"] == "online"
        assert after["archetype_overrides"].get(host) == "online"
        # the Estate classification overlay reflects the pin (archetype col agrees
        # with the VPC col) and exposes the stable identity_key handle
        arch = client.get("/api/lz/archetypes").json()
        row = next(s for s in arch["per_server"] if s.get("hostname") == host)
        assert row["archetype"] == "online"
        assert row["identity_key"] == host
    finally:
        _cleanup_nd([did], hostname=host)


def test_placement_vpc_change_clears_subnet_pin():
    """A VPC change with no subnet named reverts the server to its tier subnet in
    the new VPC (a stale subnet pin from the old VPC is dropped)."""
    host = f"ndtest-{_new_id('h')[-6:]}"
    st = open_store(get_settings().db_url)
    st.upsert_server(_srv(role="db", hostname=host, sid=_new_id("s")))
    st.close()
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    did = r.json()["design"]["design_id"]
    try:
        # pin to the web subnet first (db -> data tier, so web is a pin)
        client.post("/api/lz/network/placement",
                    json={"hostname": host, "subnet": "web"})
        assert client.get("/api/lz/network/design").json()["active"]["placements"][host]["overridden"] is True
        # change VPC to online with no subnet -> pin cleared, tier subnet in online
        pr = client.post("/api/lz/network/placement",
                         json={"hostname": host, "archetype": "online"})
        assert pr.status_code == 200, pr.text
        p = pr.json()["placement"]
        assert p["archetype"] == "online"
        assert p["subnet"] == "data"        # tier default (db), pin cleared
        assert p["overridden"] is False
    finally:
        _cleanup_nd([did], hostname=host)


def test_placement_override_rejects_unknown_archetype():
    host = f"ndtest-{_new_id('h')[-6:]}"
    st = open_store(get_settings().db_url)
    st.upsert_server(_srv(role="app", hostname=host, sid=_new_id("s")))
    st.close()
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    did = r.json()["design"]["design_id"]
    try:
        pr = client.post("/api/lz/network/placement",
                         json={"hostname": host, "archetype": "nope", "by": "test"})
        assert pr.status_code == 400
    finally:
        _cleanup_nd([did], hostname=host)


# account override — reattribute a server to a different existing account
# WITHOUT moving its VPC (account is otherwise derived 1:1 from the archetype).
# ---------------------------------------------------------------------------
def test_build_account_override_reattributes_without_moving_vpc():
    """An account pin changes the placement's account but leaves the VPC, subnet
    and IP untouched — account is decoupled from topology."""
    lz = default_lz_design()
    servers = [_srv(role="db", hostname="db1", sid="c")]   # db -> corp/corp
    nd = build_network_design(lz, servers, account_overrides={"db1": "online"})
    p = nd.placements["db1"]
    assert p["archetype"] == "corp"          # VPC unchanged
    assert p["vpc"] == "vpc-corp"            # topology unchanged
    assert p["subnet"] == "data"             # tier subnet unchanged
    assert p["account"] == "online"          # reattributed (account = archetype name)
    assert p["account_overridden"] is True
    assert p["arch_overridden"] is False
    assert nd.account_overrides == {"db1": "online"}
    assert nd.validation["ok"], nd.validation["errors"]


def test_build_account_default_follows_vpc_when_no_override():
    """Without an account pin the placement's account is the VPC's account and
    account_overridden is False."""
    lz = default_lz_design()
    servers = [_srv(role="db", hostname="db1", sid="c")]
    nd = build_network_design(lz, servers)
    p = nd.placements["db1"]
    assert p["account"] == "corp"            # account name = archetype name
    assert p["account_overridden"] is False
    assert nd.account_overrides == {}


def test_placement_override_changes_account_and_persists():
    """Activate a design, then reattribute a seeded server to a different
    existing account via the placement endpoint — the VPC stays put, the active
    design carries the account pin."""
    host = f"ndtest-{_new_id('h')[-6:]}"
    st = open_store(get_settings().db_url)
    st.upsert_server(_srv(role="db", hostname=host, sid=_new_id("s")))  # corp/corp
    st.close()
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    did = r.json()["design"]["design_id"]
    try:
        before = client.get("/api/lz/network/design").json()["active"]
        assert before["placements"][host]["account"] == "corp"
        assert before["placements"][host]["vpc"] == "vpc-corp"
        pr = client.post("/api/lz/network/placement",
                         json={"hostname": host, "account": "online", "by": "test"})
        assert pr.status_code == 200, pr.text
        place = pr.json()["placement"]
        assert place["account"] == "online"
        assert place["vpc"] == "vpc-corp"           # VPC did NOT move
        assert place["account_overridden"] is True
        # the active design (reloaded) carries the account pin
        after = client.get("/api/lz/network/design").json()["active"]
        assert after["placements"][host]["account"] == "online"
        assert after["placements"][host]["vpc"] == "vpc-corp"
        assert after["account_overrides"].get(host) == "online"
    finally:
        _cleanup_nd([did], hostname=host)


def test_placement_override_account_revert_clears_pin():
    """Sending account="" (revert) with no other change clears an existing
    account pin — the placement falls back to the VPC's account."""
    host = f"ndtest-{_new_id('h')[-6:]}"
    st = open_store(get_settings().db_url)
    st.upsert_server(_srv(role="db", hostname=host, sid=_new_id("s")))
    st.close()
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    did = r.json()["design"]["design_id"]
    try:
        client.post("/api/lz/network/placement",
                    json={"hostname": host, "account": "online", "by": "test"})
        pr = client.post("/api/lz/network/placement",
                         json={"hostname": host, "account": "", "by": "test"})
        assert pr.status_code == 200, pr.text
        assert pr.json()["placement"]["account"] == "corp"
        assert pr.json()["placement"]["account_overridden"] is False
        after = client.get("/api/lz/network/design").json()["active"]
        assert host not in (after["account_overrides"] or {})
    finally:
        _cleanup_nd([did], hostname=host)


def test_placement_override_rejects_unknown_account():
    host = f"ndtest-{_new_id('h')[-6:]}"
    st = open_store(get_settings().db_url)
    st.upsert_server(_srv(role="app", hostname=host, sid=_new_id("s")))
    st.close()
    r = client.post("/api/lz/network/design", json={"activate": True, "by": "test"})
    did = r.json()["design"]["design_id"]
    try:
        pr = client.post("/api/lz/network/placement",
                         json={"hostname": host, "account": "bogus", "by": "test"})
        assert pr.status_code == 400
    finally:
        _cleanup_nd([did], hostname=host)

# -- AI summary column (the policy ``notes`` surface as nd.summary) -----------
def test_build_network_design_summary_from_policy_notes():
    """The AI description = the carving policy's ``notes`` (MigraQ authors it).
    The deterministic default policy has no notes -> summary stays ''."""
    lz = default_lz_design()
    servers = [_srv(role="web", hostname="w1", sid="w1"),
               _srv(role="db", hostname="d1", sid="d1")]
    # LLM-proposed policy carries notes -> surfaced as the design's AI summary
    pol = dict(default_network_policy())
    pol["notes"] = "4-tier carving, /24 subnets, db+cache grouped into data"
    nd = build_network_design(lz, servers, policy=pol)
    assert nd.summary == "4-tier carving, /24 subnets, db+cache grouped into data"
    # the deterministic default policy carries no notes -> empty (honest: no AI)
    nd0 = build_network_design(lz, servers)
    assert nd0.summary == ""


def test_store_network_design_summary_roundtrips():
    """The AI ``summary`` column persists + reads back for network designs."""
    st = open_store(get_settings().db_url)
    did = _new_id("nd")
    try:
        nd = NetworkDesign(design_id=did, name="with-summary",
                           summary="MigraQ: 3-tier public/private/protected",
                           vpcs=[], placements={}, onprem_cidrs=[], accounts={},
                           policy={"notes": "x"}, overrides={}, validation={"ok": True})
        st.upsert_network_design(nd)
        got = st.get_network_design(did)
        assert got is not None
        assert got.summary == "MigraQ: 3-tier public/private/protected"
    finally:
        _cleanup_nd([did])
        st.close()


def test_network_designs_list_surfaces_summary():
    """POST /api/lz/network/design with an LLM policy carrying notes -> the saved
    design row in GET /api/lz/network/designs carries the AI summary."""
    pol = json.dumps({"subnet_prefix": 24, "tier_order": list(TIER_ORDER),
                      "tiers": {"web": "web"}, "notes": "/24 subnets, default tiers"})
    with patch.object(m.LLM, "chat", return_value=pol):
        r = client.post("/api/lz/network/design",
                        json={"demand": "/24 subnets", "activate": True, "by": "test"})
    assert r.status_code == 200, r.text
    did = r.json()["design"]["design_id"]
    try:
        assert r.json()["design"]["summary"] == "/24 subnets, default tiers"
        listed = client.get("/api/lz/network/designs").json()
        row = next(d for d in listed["designs"] if d["design_id"] == did)
        assert row["summary"] == "/24 subnets, default tiers"
    finally:
        _cleanup_nd([did])
