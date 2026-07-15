"""LZ scale tiers (small/medium/large) — the default-strategy axis.

The scale tier drives the DEFAULT STRATEGY (the comparison matrix: account /
networking / security / audit / budgeting + topology shape + archetype count).
The operator-customizable parts (app placement, CIDRs, names, compliance tiers)
flow through the AI prompt (the LLM interview). The 3 cross-scale golden rules
(no long-term AK/SK, cross-account ActionTrail, 10x CIDR headroom) are invariants
the validator surfaces as warnings regardless of tier.
"""
import json
from unittest.mock import patch

from idc.config import Settings
from idc.llm.client import LLMClient, _lz_estate_context, _lz_design_system
from idc.core.lz import (
    LZ_SMALL, LZ_MEDIUM, LZ_LARGE, LZ_SCALE_TIERS, LZ_GOLDEN_RULES,
    scale_for, estate_scale_metrics, default_lz_design,
    default_lz_design_for_scale, validate_lz_design, _governance_for_scale,
    _blueprints_for_scale,
)
from idc.core.models import LZDesign, Server


def _srv(role="", tags=None, sid="s", app_ids=None, n=1, cores=0, mem_gb=0,
         source_type="vm"):
    return [Server(id=f"{sid}{i}", hostname=f"{sid}{i}", role=role,
                   tags=tags or [], app_ids=app_ids or [], cpu_cores=cores,
                   mem_gb=mem_gb, source_type=source_type) for i in range(n)]


# -- scale_for (deterministic estate inference) ------------------------------
def test_scale_for_small():
    # < 5 apps and < 50 servers, no regulated tag -> small
    assert scale_for(_srv(role="web", app_ids=["A"], n=3)) == LZ_SMALL


def test_scale_for_medium():
    # >= 5 apps or >= 50 servers, no regulated tag -> medium
    servers = []
    for i in range(60):
        servers.append(Server(id=f"s{i}", hostname=f"h{i}", role="web",
                              app_ids=[f"APP_{i % 6}"]))
    assert scale_for(servers) == LZ_MEDIUM


def test_scale_for_large_by_size():
    # > 2000 servers -> large
    servers = [Server(id=f"s{i}", hostname=f"h{i}", role="db",
                      app_ids=["A"]) for i in range(2500)]
    assert scale_for(servers) == LZ_LARGE


def test_scale_for_large_by_apps():
    # > 30 apps -> large
    servers = [Server(id=f"s{i}", hostname=f"h{i}", role="web",
                      app_ids=[f"APP_{i}"]) for i in range(31)]
    assert scale_for(servers) == LZ_LARGE


def test_scale_for_large_by_regulated_tag():
    # any regulated tag forces large regardless of size (compliance needs governance)
    assert scale_for(_srv(role="db", tags=["pci"], n=1)) == LZ_LARGE
    assert scale_for(_srv(role="web", tags=["idc:dmz"], n=1)) == LZ_LARGE


def test_scale_for_override_wins():
    servers = [Server(id=f"s{i}", hostname=f"h{i}", role="db",
                      app_ids=["A"]) for i in range(2500)]
    # the operator pinned small in the UI -> small, even for a huge estate
    assert scale_for(servers, scale_override=LZ_SMALL) == LZ_SMALL
    # an unknown override is ignored, falls back to inference
    assert scale_for(_srv(role="web", n=3), scale_override="huge") == LZ_SMALL


def test_scale_for_large_by_cores():
    # few hosts but many cores (>8000) -> large (a fat-but-few estate scales up)
    fat = _srv(role="db", n=3, cores=3000)   # 3 hosts * 3000 = 9000 cores
    assert scale_for(fat) == LZ_LARGE


def test_scale_for_medium_by_cores():
    # 3 hosts, 100 cores each = 300 cores -> medium (>=200) even though <50 hosts
    assert scale_for(_srv(role="web", n=3, cores=100)) == LZ_MEDIUM


def test_scale_for_small_when_tiny_and_thin():
    # 3 hosts, no cores recorded, no apps -> small
    assert scale_for(_srv(role="web", n=3)) == LZ_SMALL


# -- estate_scale_metrics (the concrete sizing signals) ---------------------
def test_estate_scale_metrics_sums_cores_and_memory():
    servers = [Server(id="a", hostname="a", role="db", cpu_cores=16, mem_gb=64,
                      source_type="baremetal", env="prod", app_ids=["A1"]),
               Server(id="b", hostname="b", role="web", cpu_cores=8, mem_gb=16,
                      source_type="vm", env="dev", app_ids=["A1", "A2"]),
               Server(id="c", hostname="c", role="web", cpu_cores=4, mem_gb=8,
                      source_type="vm", env="dev", tags=["pci"])]
    m = estate_scale_metrics(servers)
    assert m["servers"] == 3
    assert m["cores"] == 28
    assert m["memory_gb"] == 88
    assert m["baremetal"] == 1
    assert m["apps"] == 2              # A1, A2 (deduped across hosts)
    assert m["regulated"] is True      # the pci tag
    assert m["by_env"] == {"dev": 2, "prod": 1}


def test_scale_for_accepts_precomputed_metrics():
    m = {"servers": 3000, "apps": 1, "cores": 100, "memory_gb": 0,
         "baremetal": 0, "regulated": False, "by_env": {}}
    # uses the passed metrics (no re-scan) — 3000 servers -> large
    assert scale_for([], metrics=m) == LZ_LARGE


def test_scale_tiers_carry_estate_size_bands():
    # every tier exposes the sizing bands the UI matches the estate against
    for t in (LZ_SMALL, LZ_MEDIUM, LZ_LARGE):
        bands = LZ_SCALE_TIERS[t]["estate_size"]
        assert {"servers", "cores", "memory_gb", "accounts", "apps"} <= set(bands)


# -- default_lz_design_for_scale (the built-in scale defaults) ---------------
def test_default_designs_valid_for_all_tiers():
    for tier in (LZ_SMALL, LZ_MEDIUM, LZ_LARGE):
        d = default_lz_design_for_scale(tier)
        assert d.scale == tier
        v = validate_lz_design(d)
        assert v["ok"] is True, f"{tier}: {v['errors']}"
        # governance is filled by the scale defaults (no missing-block warning)
        assert "governance block missing" not in " ".join(v["warnings"])
        # requirements record the scale + governance + the applied default matrix
        assert d.requirements["scale"] == tier
        assert "governance" in d.requirements
        assert d.requirements["defaults_applied"]["scale"] == tier


def test_small_default_is_flat_two_archetype_no_dmz():
    d = default_lz_design_for_scale(LZ_SMALL)
    assert set(d.archetypes) == {"corp", "online"}   # no dmz tier (flat)
    assert "dmz" not in d.archetypes


def test_medium_default_is_hub_spoke_three_archetype():
    d = default_lz_design_for_scale(LZ_MEDIUM)
    assert set(d.archetypes) == {"corp", "online", "dmz"}


def test_large_default_adds_cfw_and_scp_guardrails():
    d = default_lz_design_for_scale(LZ_LARGE)
    corp_pol = d.archetypes["corp"]["policy_as_code"]
    assert any("Cloud Firewall" in p for p in corp_pol)
    assert any("SCP guardrail" in p for p in corp_pol)
    # medium has neither
    m = default_lz_design_for_scale(LZ_MEDIUM)
    assert not any("Cloud Firewall" in p for p in m.archetypes["corp"]["policy_as_code"])


def test_default_lz_design_backcompat_is_medium():
    """default_lz_design() (no scale arg) stays the medium built-in — callers
    and tests that predate the scale tiers keep working."""
    d = default_lz_design()
    assert d.scale == LZ_MEDIUM
    assert set(d.archetypes) == {"corp", "online", "dmz"}


def test_governance_centralized_logging_scales_with_tier():
    assert _governance_for_scale(LZ_SMALL)["audit"]["cross_account"] is False
    assert _governance_for_scale(LZ_SMALL)["audit"]["destination_account"] == "self"
    assert _governance_for_scale(LZ_MEDIUM)["audit"]["cross_account"] is True
    assert _governance_for_scale(LZ_MEDIUM)["audit"]["destination_account"] == "logging"
    # large tier federates identity via IdP and turns on SIEM + auto-remediation
    g = _governance_for_scale(LZ_LARGE)
    assert g["identity"]["federation"] == "idp_oidc_saml"
    assert g["audit"]["siem"] is True
    assert g["audit"]["automated_remediation"] is True
    assert g["network"]["cfw_inspection"] is True


# -- validate_lz_design golden-rule warnings ---------------------------------
def test_validate_warns_on_missing_governance():
    d = LZDesign(name="t", archetypes={
        "x": {"archetype": "x", "vpc": {"name": "v", "cidr": "10.90.0.0/16"},
              "peering": {}, "internet_egress": {}, "security_groups": [],
              "cam_roles": [], "tag_policy": {}, "policy_as_code": ["r"]}},
        requirements={}, onprem_cidrs=[])
    v = validate_lz_design(d)
    assert v["ok"] is True               # missing governance is a WARNING, not an error
    assert any("governance block missing" in w for w in v["warnings"])


def test_validate_warns_on_tiny_cidr_10x_headroom():
    d = LZDesign(name="t", archetypes={
        "x": {"archetype": "x", "vpc": {"name": "v", "cidr": "10.90.0.0/28"},
              "peering": {}, "internet_egress": {}, "security_groups": [],
              "cam_roles": [], "tag_policy": {}, "policy_as_code": ["r"]}},
        requirements={"scale": LZ_MEDIUM,
                      "governance": _governance_for_scale(LZ_MEDIUM)},
        onprem_cidrs=[])
    v = validate_lz_design(d)
    assert v["ok"] is True
    assert any("10x growth" in w for w in v["warnings"])


def test_validate_warns_on_identity_violation():
    g = _governance_for_scale(LZ_MEDIUM)
    g["identity"]["mode"] = "long_term_aksk"
    d = LZDesign(name="t", archetypes={
        "x": {"archetype": "x", "vpc": {"name": "v", "cidr": "10.90.0.0/16"},
              "peering": {}, "internet_egress": {}, "security_groups": [],
              "cam_roles": [], "tag_policy": {}, "policy_as_code": ["r"]}},
        requirements={"scale": LZ_MEDIUM, "governance": g}, onprem_cidrs=[])
    v = validate_lz_design(d)
    assert v["ok"] is True
    assert any("identity rule violated" in w for w in v["warnings"])


def test_validate_returns_warnings_key_backcompat():
    """Callers that read v['ok'] / v['errors'] still work; warnings is additive."""
    v = validate_lz_design(default_lz_design())
    assert set(("ok", "errors", "warnings")).issubset(v.keys())


# -- design_lz stamps the scale + governance (mocked LLM) --------------------
def _valid_design():
    """A minimal valid LLM proposal the validator accepts (no governance — the
    engine must fill it from the scale defaults)."""
    return {
        "name": "test", "onprem_cidrs": ["172.16.0.0/12"],
        "requirements": {"classifier": {"role_map": {"web": "spoke"},
                                        "default": "hub"}},
        "archetypes": {
            "hub": {"archetype": "hub", "summary": "h",
                    "vpc": {"name": "vpc-h", "cidr": "10.10.0.0/16", "description": "h"},
                    "peering": {"to_spoke": True},
                    "internet_egress": {"nat": True, "public_clb": False},
                    "security_groups": ["sg"], "cam_roles": ["r"],
                    "tag_policy": {"env": "required"},
                    "policy_as_code": ["require env tag"]},
            "spoke": {"archetype": "spoke", "summary": "s",
                      "vpc": {"name": "vpc-s", "cidr": "10.20.0.0/16", "description": "s"},
                      "peering": {"to_hub": True},
                      "internet_egress": {"nat": True, "public_clb": True},
                      "security_groups": ["sg"], "cam_roles": ["r"],
                      "tag_policy": {"env": "required"},
                      "policy_as_code": ["public clb behind waf"]}},
    }


def _client():
    return LLMClient(Settings(llm_enabled=True))


def test_design_lz_stamps_inferred_scale_and_governance():
    """With no scale override, design_lz infers it from the estate and stamps it
    on the returned design + fills requirements.governance from the tier defaults."""
    c = _client()
    # 60 servers / 6 apps -> medium
    servers = [Server(id=f"s{i}", hostname=f"h{i}", role="web",
                      app_ids=[f"APP_{i % 6}"]) for i in range(60)]
    with patch.object(c, "chat", return_value=json.dumps(_valid_design())):
        r = c.design_lz("hub and spoke", servers)
    assert r["ok"] is True
    d = r["design"]
    assert d.scale == LZ_MEDIUM
    assert d.requirements["scale"] == LZ_MEDIUM
    # governance filled from the medium defaults even though the LLM omitted it
    assert d.requirements["governance"]["identity"]["mode"] == "sso_role_only"
    assert d.requirements["governance"]["audit"]["cross_account"] is True


def test_design_lz_scale_override_wins_over_inference():
    c = _client()
    big = [Server(id=f"s{i}", hostname=f"h{i}", role="db",
                  app_ids=["A"]) for i in range(2500)]   # would infer large
    with patch.object(c, "chat", return_value=json.dumps(_valid_design())):
        r = c.design_lz("flat small layout", big, scale=LZ_SMALL)
    assert r["ok"] is True
    assert r["design"].scale == LZ_SMALL
    # small tier -> local ActionTrail, no cross-account logging
    assert r["design"].requirements["governance"]["audit"]["cross_account"] is False


def test_design_lz_llm_governance_override_merges_with_scale_defaults():
    """If the LLM emits a governance key (operator asked for one via the prompt),
    it wins per-key; the scale defaults fill the rest."""
    c = _client()
    design = _valid_design()
    design["requirements"]["governance"] = {
        "identity": {"mode": "custom_idp", "rule": "corp IdP only"}}
    servers = [Server(id=f"s{i}", hostname=f"h{i}", role="web",
                      app_ids=[f"APP_{i % 6}"]) for i in range(60)]
    with patch.object(c, "chat", return_value=json.dumps(design)):
        r = c.design_lz("use our corp IdP", servers)
    assert r["ok"] is True
    g = r["design"].requirements["governance"]
    assert g["identity"]["mode"] == "custom_idp"   # LLM override kept
    assert g["audit"]["cross_account"] is True      # scale default filled the rest


# -- prompt construction is scale-aware --------------------------------------
def test_lz_design_system_includes_scale_block_and_golden_rules():
    p = _lz_design_system(LZ_LARGE)
    assert "SCALE TIER" in p
    assert "Large — Enterprise" in p
    assert "Cloud Firewall" in p               # large-tier extra
    # all 3 golden rules appear, keyed
    for r in LZ_GOLDEN_RULES:
        assert r["key"] in p and r["rule"][:30] in p
    assert "USER-MODIFIABLE" in p              # the customizable-vs-default split


def test_lz_design_system_small_is_flat_no_cfw():
    p = _lz_design_system(LZ_SMALL)
    assert "Flat 2-VPC layout" in p
    assert "Cloud Firewall" not in p


def test_lz_estate_context_carries_scale_tier():
    servers = [Server(id=f"s{i}", hostname=f"h{i}", role="db",
                      app_ids=["A"]) for i in range(2500)]
    ctx = _lz_estate_context(servers, onprem_cidrs=["10.0.0.0/16"])
    assert '"scale_tier": "large"' in ctx
    assert '"scale_label"' in ctx


def test_lz_estate_context_scale_override():
    servers = [Server(id=f"s{i}", hostname=f"h{i}", role="db",
                      app_ids=["A"]) for i in range(2500)]
    ctx = _lz_estate_context(servers, onprem_cidrs=["10.0.0.0/16"], scale=LZ_SMALL)
    assert '"scale_tier": "small"' in ctx