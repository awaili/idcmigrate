"""Tests for the 7R migration-strategy capability (LLM + codeintel ranking)."""
import json
from unittest.mock import patch

import pytest

from idc.config import Settings
from idc.core.codeintel import enrich_match, non_migrating_servers, pattern_rank
from idc.core.llm_plan import WavePolicy, build_from_policy
from idc.core.models import (
    ALL_PATTERNS_7R, AppStrategy, CodeProfile, Match, MIGRATING_PATTERNS,
    NON_MIGRATING_PATTERNS, PATTERN_RETAIN, PATTERN_RETIRE, Server,
    STRATEGY_SOURCE_AI, STAGE_RETAIN, STAGE_RETIRE, Utilization, Workload,
)
from idc.core.plan import plan_waves
from idc.llm.client import LLMClient


# ---------------------------------------------------------------------------
# vocabulary / ranking
# ---------------------------------------------------------------------------
def test_7r_vocabulary_is_seven():
    assert len(ALL_PATTERNS_7R) == 7
    # the 7R framework: rehost, rehost-container, replatform, refactor,
    # repurchase, retire, relocate. (retain is a keep-on-prem disposition,
    # not one of the seven — the strategist may still return it.)
    for p in ("rehost", "rehost-container", "replatform", "refactor",
              "repurchase", "retire", "relocate"):
        assert p in ALL_PATTERNS_7R
    assert "retain" not in ALL_PATTERNS_7R   # retain is a disposition, not a 7R
    # the LLM strategist's valid set is the 7R PLUS retain
    from idc.core.models import STRATEGY_CHOICES
    assert set(STRATEGY_CHOICES) == set(ALL_PATTERNS_7R) | {"retain"}


def test_pattern_rank_orders_cheapest_first_and_non_migrating_last():
    ranks = {p: pattern_rank(CodeProfile(app_id="x", migration_pattern=p))
             for p in ALL_PATTERNS_7R}
    assert ranks["rehost"] == 0
    assert ranks["rehost-container"] == 1                 # between rehost and replatform
    assert ranks["replatform"] == 2
    assert ranks["relocate"] == 0                          # no-code-change move, as cheap as rehost
    # migrating patterns (the 7R minus retire) sort before the non-migrating
    # dispositions (retain/retire), which rank >= 10.
    for m in MIGRATING_PATTERNS:
        assert pattern_rank(CodeProfile(app_id="x", migration_pattern=m)) < 10
    retain_r = pattern_rank(CodeProfile(app_id="x", migration_pattern="retain"))
    retire_r = pattern_rank(CodeProfile(app_id="x", migration_pattern="retire"))
    assert retain_r >= 10 and retire_r >= 10
    # every migrating pattern sorts before retain/retire
    for m in MIGRATING_PATTERNS:
        assert pattern_rank(CodeProfile(app_id="x", migration_pattern=m)) < retain_r


def test_enrich_match_retain_floors_confidence_and_notes():
    m = Match(server_id="s", confidence=0.9, rationale="base")
    enrich_match(m, CodeProfile(app_id="a", migration_pattern="retain"))
    assert m.confidence <= 0.1
    assert "do not migrate" in m.rationale


def test_enrich_match_retire_floors_confidence_and_notes():
    m = Match(server_id="s", confidence=0.9, rationale="base")
    enrich_match(m, CodeProfile(app_id="a", migration_pattern="retire"))
    assert m.confidence <= 0.1
    assert "decommission" in m.rationale


def test_enrich_match_rehost_container_not_flagged_as_requires():
    # rehost-container is mostly packaging — should NOT add a "requires ..." note
    m = Match(server_id="s", confidence=0.9, rationale="base")
    enrich_match(m, CodeProfile(app_id="a", migration_pattern="rehost-container",
                                cloud_readiness=0.9))
    assert "requires" not in m.rationale


# ---------------------------------------------------------------------------
# LLM seven_r_strategy
# ---------------------------------------------------------------------------
def _estate():
    s = Server(hostname="web-1", role="app", os="centos", app_ids=["app-1"],
               utilization=Utilization(cpu_p95=40, mem_p95=50, disk_used_pct=30))
    wls = [Workload(app_id="app-1", name="web", tier="frontend",
                   server_ids=[s.id], depends_on=["app-db"])]
    prof = CodeProfile(app_id="app-1", language="java", runtime="jdk8",
                       framework="spring", cloud_readiness=0.7,
                       migration_pattern="rehost")
    return s, wls, prof


def _resp(strategy, **kw):
    d = {"strategy": strategy, "rationale": "r", "target": "TKE",
         "confidence": 0.7, "effort": "low", "key_changes": ["add Dockerfile"]}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def test_seven_r_strategy_parses_valid():
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp("rehost-container")):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is True
    assert r["strategy"] == "rehost-container"
    assert r["target"] == "TKE"
    assert r["confidence"] == pytest.approx(0.7)
    assert r["key_changes"] == ["add Dockerfile"]


@pytest.mark.parametrize("strat", list(ALL_PATTERNS_7R))
def test_seven_r_strategy_accepts_each_of_7r(strat):
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp(strat)):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is True and r["strategy"] == strat


def test_seven_r_strategy_accepts_retain_disposition():
    """retain is NOT one of the 7R, but the strategist may still return it
    (keep-on-prem) — so a regulated mainframe isn't forced into a migrating R."""
    from idc.core.models import STRATEGY_CHOICES
    assert "retain" in STRATEGY_CHOICES
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp("retain")):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is True and r["strategy"] == "retain"


def test_enrich_match_relocate_is_clean_no_floor_no_requires_note():
    """relocate is a no-code-change move (like rehost): no confidence floor, no
    'requires relocate' note (it's not extra work)."""
    m = Match(server_id="s", confidence=0.9, rationale="base")
    enrich_match(m, CodeProfile(app_id="a", migration_pattern="relocate",
                                cloud_readiness=0.9))
    assert m.confidence == 0.9                       # no floor
    assert "requires" not in (m.rationale or "")
    assert "relocate" not in (m.rationale or "")      # no spurious note


def test_seven_r_strategy_rejects_invalid_strategy():
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp("lift-n-shift")):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is False
    assert "invalid strategy" in r["error"]


def test_seven_r_strategy_rejects_non_json():
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value="sorry, I cannot help"):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is False
    assert "not valid JSON" in r["error"]


def test_seven_r_strategy_handles_fenced_json():
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    fenced = "```json\n" + _resp("replatform") + "\n```"
    with patch.object(c, "chat", return_value=fenced):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is True and r["strategy"] == "replatform"


def test_seven_r_strategy_llm_disabled():
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=False))
    r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is False
    assert r["strategy"] == ""


def test_seven_r_strategy_llm_exception_degrades():
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", side_effect=RuntimeError("gateway down")):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is False
    assert "MigraQ error" in r["error"]


# ---------------------------------------------------------------------------
# LLM seven_r_host — per-host 7R analysis (sibling of seven_r_strategy)
# ---------------------------------------------------------------------------
from idc.core.models import Target


def _host_match(s):
    return Match(server_id=s.id, confidence=0.9, rationale="rule",
                 target=Target(product="CVM", spec="SA2.LARGE4", region="ap-bangkok"))


def test_seven_r_host_parses_valid():
    s, _wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp("rehost-container")):
        r = c.seven_r_host(s, _host_match(s), prof, current="rehost",
                          host_disposition="")
    assert r["ok"] is True
    assert r["strategy"] == "rehost-container"
    assert r["target"] == "TKE"
    assert r["confidence"] == pytest.approx(0.7)
    assert r["key_changes"] == ["add Dockerfile"]


def test_seven_r_host_context_carries_current_and_disposition():
    """The per-host context sent to the LLM includes the host's current 7R +
    any operator retain/retire override, so the LLM can respect an operator
    call (it sees the host is already marked retain)."""
    s, _wls, _prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    sent = {}
    def fake_chat(messages, stream=False, timeout=None):
        sent["user"] = messages[-1]["content"]
        return _resp("retain")
    with patch.object(c, "chat", side_effect=fake_chat):
        c.seven_r_host(s, _host_match(s), None, current="rehost",
                       host_disposition="retain")
    assert '"current_7r": "rehost"' in sent["user"]
    assert '"host_disposition": "retain"' in sent["user"]


def test_seven_r_host_rejects_invalid_strategy():
    s, _wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp("lift-n-shift")):
        r = c.seven_r_host(s, _host_match(s), prof)
    assert r["ok"] is False
    assert "invalid strategy" in r["error"]


def test_seven_r_host_llm_disabled():
    s, _wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=False))
    r = c.seven_r_host(s, _host_match(s), prof)
    assert r["ok"] is False and r["strategy"] == ""


def test_seven_r_host_llm_exception_degrades():
    s, _wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", side_effect=RuntimeError("gateway down")):
        r = c.seven_r_host(s, _host_match(s), prof)
    assert r["ok"] is False
    assert "MigraQ error" in r["error"]


# ---------------------------------------------------------------------------
# F2 — deterministic 7R confidence ceiling (clamps the LLM value)
# ---------------------------------------------------------------------------
from idc.core.codeintel import seven_r_confidence


def test_seven_r_confidence_base_is_cloud_readiness_when_clean():
    # no blockers, low effort, well-known estate -> ceiling == cloud_readiness
    assert seven_r_confidence(0.7, 0, "low", 1.0) == pytest.approx(0.7)


def test_seven_r_confidence_blockers_trim_ceiling():
    assert seven_r_confidence(0.8, 0, "low", 1.0) == pytest.approx(0.8)
    assert seven_r_confidence(0.8, 2, "low", 1.0) == pytest.approx(0.8 - 0.30)
    # capped at 3 blockers
    assert seven_r_confidence(0.8, 9, "low", 1.0) == pytest.approx(0.8 - 0.45)


def test_seven_r_confidence_high_effort_and_thin_estate_trim():
    clean = seven_r_confidence(0.9, 0, "low", 1.0)
    high_effort = seven_r_confidence(0.9, 0, "high", 1.0)
    thin_estate = seven_r_confidence(0.9, 0, "low", 0.0)
    assert high_effort < clean
    assert thin_estate < clean
    # never below the floor
    assert seven_r_confidence(0.0, 9, "high", 0.0) >= 0.05
    # never above the cap
    assert seven_r_confidence(1.0, 0, "low", 1.0) <= 0.95


def test_seven_r_strategy_clamps_overconfident_llm():
    """An LLM stamping 0.95 on a blocked, thinly-known app is clamped to the
    deterministic ceiling; the LLM may lower but not raise above it."""
    s, wls, _ = _estate()
    # blockered, high-effort, low-readiness profile
    prof = CodeProfile(app_id="app-1", cloud_readiness=0.4,
                       migration_pattern="refactor", refactor_effort="high",
                       blockers=["PL/SQL has no equivalent", "cron assumed on host"])
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp("refactor", confidence=0.95)):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is True
    assert r["confidence"] < 0.95
    assert r["confidence"] <= r["confidence_ceiling"] + 1e-9
    assert r["confidence_clamped"] is True


def test_seven_r_strategy_passes_llm_value_when_under_ceiling():
    """When the LLM is *less* confident than the ceiling, its value is kept
    (the LLM sees nuance the formula can't)."""
    s, wls, prof = _estate()   # cloud_readiness 0.7, low effort, no blockers
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp("rehost", confidence=0.3)):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] is True
    assert r["confidence"] == pytest.approx(0.3)
    assert r["confidence_clamped"] is False


# ---------------------------------------------------------------------------
# retain/retire exclusion from migration waves
# ---------------------------------------------------------------------------
def _S(sid, host, role, apps):
    return Server(id=sid, hostname=host, role=role, app_ids=apps)


def _excl_estate():
    """3 apps: A migrate (rehost), B retain, C retire; s3 is shared by A (db)."""
    servers = [
        _S("s1", "k8s-m", "k8s", ["app-A"]), _S("s2", "mon", "monitoring", ["app-A"]),
        _S("s3", "db-1", "db", ["app-A"]),    # shared with migrating app A
        _S("s4", "web-a", "app", ["app-A"]),
        _S("s5", "web-b", "app", ["app-B"]),  # retain-only
        _S("s6", "web-c", "app", ["app-C"]),  # retire-only
    ]
    wls = [
        Workload(app_id="app-A", name="A", server_ids=["s3", "s4"]),
        Workload(app_id="app-B", name="B", server_ids=["s5"], depends_on=["app-A"]),
        Workload(app_id="app-C", name="C", server_ids=["s6"]),
    ]
    profiles = [
        CodeProfile(app_id="app-A", migration_pattern="rehost"),
        CodeProfile(app_id="app-B", migration_pattern="retain"),
        CodeProfile(app_id="app-C", migration_pattern="retire"),
    ]
    return servers, wls, profiles


def test_non_migrating_servers_pure_disposition():
    servers, wls, profiles = _excl_estate()
    retain, retire = non_migrating_servers(servers, wls, profiles)
    assert retain == {"s5"}
    assert retire == {"s6"}


def test_non_migrating_servers_shared_with_migrating_stays():
    # s3 belongs to app-A (migrate) -> must NOT be retain/retire even if we also
    # tag another of its apps retain. Here s3 has only app-A (migrate) so it migrates.
    servers, wls, profiles = _excl_estate()
    retain, retire = non_migrating_servers(servers, wls, profiles)
    assert "s3" not in retain and "s3" not in retire


def test_non_migrating_servers_no_app_binding_not_included():
    s = Server(id="sX", hostname="orphan", role="app", app_ids=[])
    retain, retire = non_migrating_servers([s], [], [])
    assert retain == set() and retire == set()


def test_plan_waves_routes_retain_retire_to_trailing_waves():
    servers, wls, profiles = _excl_estate()
    waves = plan_waves(servers, wls, code_profiles=profiles)
    retain = next((w for w in waves if w.stage == STAGE_RETAIN), None)
    retire = next((w for w in waves if w.stage == STAGE_RETIRE), None)
    assert retain is not None and retain.server_ids == ["s5"]
    assert retire is not None and retire.server_ids == ["s6"]
    # retain/retire servers never appear in a migration-stage wave
    mig_sids = {s for w in waves if w.stage not in (STAGE_RETAIN, STAGE_RETIRE)
                for s in w.server_ids}
    assert "s5" not in mig_sids and "s6" not in mig_sids
    # migrate app servers are still placed
    assert "s3" in mig_sids and "s4" in mig_sids


def test_build_from_policy_excludes_retain_retire():
    servers, wls, profiles = _excl_estate()
    pol = WavePolicy.from_dict({"notes": "t", "stages": [
        {"label": "Apps", "stage": "3_application", "group_by": "app",
         "use_app_deps": True}]})
    rep = build_from_policy(pol, servers, wls, matches=None, profiles=profiles)
    stages = [w.stage for w in rep.waves]
    assert STAGE_RETAIN in stages and STAGE_RETIRE in stages
    app_sids = {s for w in rep.waves if w.stage == "3_application"
                for s in w.server_ids}
    assert "s5" not in app_sids and "s6" not in app_sids   # excluded from app stage
    # but they ARE placed (in their disposition waves) — every server accounted for
    placed = {s for w in rep.waves for s in w.server_ids}
    assert {"s5", "s6"} <= placed


def test_build_from_policy_no_profiles_is_unchanged():
    # no profiles -> no retain/retire waves (back-compat)
    servers, wls, _ = _excl_estate()
    pol = WavePolicy.from_dict({"notes": "t", "stages": [
        {"label": "Apps", "stage": "3_application", "group_by": "app",
         "use_app_deps": True}]})
    rep = build_from_policy(pol, servers, wls, matches=None, profiles=None)
    stages = [w.stage for w in rep.waves]
    assert STAGE_RETAIN not in stages and STAGE_RETIRE not in stages


# ---------------------------------------------------------------------------
# strategies overlay (AI 7R) — #3/#4: separate from executor pattern, drives
# wave exclusion WITHOUT being written to CodeProfile.migration_pattern.
# ---------------------------------------------------------------------------
def test_non_migrating_servers_strategy_overlay_drives_disposition():
    """An AI strategy overlay (retain) excludes the app's servers — even when
    no CodeProfile exists. This is the #4 capability: 7R drives exclusion
    straight from the overlay, no DB persistence needed."""
    servers, wls, _ = _excl_estate()   # app-B -> s5, app-C -> s6
    strategies = {
        "app-B": AppStrategy(app_id="app-B", strategy=PATTERN_RETAIN),
        "app-C": AppStrategy(app_id="app-C", strategy=PATTERN_RETIRE),
    }
    retain, retire = non_migrating_servers(servers, wls, profiles=[], strategies=strategies)
    assert retain == {"s5"} and retire == {"s6"}


def test_non_migrating_servers_overlay_overrides_legacy_profile():
    """#3: the AI overlay is authoritative. If the overlay says app-B is rehost
    (migrate) but a legacy CodeProfile.migration_pattern says retain, the AI
    wins — app-B migrates (not retained). The executor's pattern is NOT
    overwritten, but it doesn't override the AI decision either."""
    servers, wls, _ = _excl_estate()
    profiles = [CodeProfile(app_id="app-B", migration_pattern=PATTERN_RETAIN)]
    strategies = {"app-B": AppStrategy(app_id="app-B", strategy="rehost")}
    retain, retire = non_migrating_servers(servers, wls, profiles, strategies)
    assert retain == set() and retire == set()   # AI says migrate -> nothing retained


def test_non_migrating_servers_legacy_profile_fallback():
    """When NO overlay is given, the legacy CodeProfile.migration_pattern retain
    still works (back-compat with older 7R --apply that wrote to CodeProfile)."""
    servers, wls, _ = _excl_estate()
    profiles = [CodeProfile(app_id="app-B", migration_pattern=PATTERN_RETAIN),
                CodeProfile(app_id="app-C", migration_pattern=PATTERN_RETIRE)]
    retain, retire = non_migrating_servers(servers, wls, profiles, strategies=None)
    assert retain == {"s5"} and retire == {"s6"}


def test_plan_waves_accepts_strategies_overlay():
    """plan_waves honors a strategies overlay directly (no DB write needed)."""
    servers, wls, _ = _excl_estate()
    strategies = {"app-B": AppStrategy(app_id="app-B", strategy=PATTERN_RETAIN),
                  "app-C": AppStrategy(app_id="app-C", strategy=PATTERN_RETIRE)}
    waves = plan_waves(servers, wls, strategies=strategies)
    retain = next(w for w in waves if w.stage == STAGE_RETAIN)
    retire = next(w for w in waves if w.stage == STAGE_RETIRE)
    assert retain.server_ids == ["s5"] and retire.server_ids == ["s6"]


def test_build_from_policy_accepts_strategies_overlay():
    servers, wls, _ = _excl_estate()
    pol = WavePolicy.from_dict({"notes": "t", "stages": [
        {"label": "Apps", "stage": "3_application", "group_by": "app",
         "use_app_deps": True}]})
    strategies = {"app-B": AppStrategy(app_id="app-B", strategy=PATTERN_RETAIN),
                  "app-C": AppStrategy(app_id="app-C", strategy=PATTERN_RETIRE)}
    rep = build_from_policy(pol, servers, wls, matches=None, strategies=strategies)
    stages = [w.stage for w in rep.waves]
    assert STAGE_RETAIN in stages and STAGE_RETIRE in stages


def test_seven_r_strategy_apply_does_not_overwrite_executor_pattern():
    """#3: the 7R result is an AppStrategy (separate table) — the executor's
    CodeProfile.migration_pattern is untouched. We verify the LLM result dict
    carries the structured fields the store needs, and that a CodeProfile with
    the executor's pattern stays intact when we only build an AppStrategy."""
    s, wls, prof = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    with patch.object(c, "chat", return_value=_resp("retain")):
        r = c.seven_r_strategy("app-1", [s], wls, [], [prof])
    assert r["ok"] and r["strategy"] == "retain"
    # the executor's profile pattern is unchanged by the AI call
    assert prof.migration_pattern == "rehost"
    # the AppStrategy built from the result is the thing to persist — separate
    strat = AppStrategy(app_id=r["app_id"], strategy=r["strategy"],
                        rationale=r["rationale"], target=r["target"],
                        confidence=r["confidence"], effort=r["effort"],
                        key_changes=r["key_changes"], source=STRATEGY_SOURCE_AI)
    assert strat.strategy == "retain" and strat.source == STRATEGY_SOURCE_AI


def test_app_strategy_roundtrip_dict():
    """AppStrategy <-> dict roundtrip (the shape the store + API use)."""
    s = AppStrategy(app_id="app-X", strategy="replatform", rationale="use managed DB",
                    target="CDB", confidence=0.8, effort="medium",
                    key_changes=["swap jdbc url", "drop self-hosted mysql"],
                    source=STRATEGY_SOURCE_AI)
    d = s.to_dict()
    assert d["strategy"] == "replatform" and d["key_changes"] == ["swap jdbc url", "drop self-hosted mysql"]
    s2 = AppStrategy.from_dict(d)
    assert s2.app_id == "app-X" and s2.strategy == "replatform"
    assert s2.key_changes == ["swap jdbc url", "drop self-hosted mysql"]
    assert s2.source == STRATEGY_SOURCE_AI
    # from_dict tolerates missing fields (defaults)
    s3 = AppStrategy.from_dict({"app_id": "app-Y", "strategy": "retain"})
    assert s3.app_id == "app-Y" and s3.strategy == "retain" and s3.key_changes == []