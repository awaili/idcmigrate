"""Tests for the LLM wave-policy planner — focus on the max_waves revision loop.

All tests mock ``LLMClient.chat`` so no network/gateway is hit. The estate is
built inline (no fixture pipeline) to keep these fast and isolated.
"""
import json
from unittest.mock import patch

import pytest

from idc.config import Settings
from idc.core.llm_plan import build_from_policy, cap_waves, validate_plan, WavePolicy
from idc.core.models import Server, Utilization, Wave, Workload
from idc.llm import get_planner
from idc.llm.planner import MAX_REVISIONS, _parse_max_waves


# ---------------------------------------------------------------------------
# estate builder
# ---------------------------------------------------------------------------
def _estate():
    """15 servers: 2 k8s, 2 db/cache, 11 app across 5 apps (for group_by=app)."""
    servers = []
    servers.append(Server(hostname="k8s-m", role="k8s", os="centos"))
    servers.append(Server(hostname="mon-g", role="monitoring", os="centos"))
    servers.append(Server(hostname="db-1", role="db", os="centos", app_ids=["app-data"]))
    servers.append(Server(hostname="cache-1", role="cache", os="centos", app_ids=["app-data"]))
    # 5 apps, 1-3 servers each, 11 total
    for ai, app in enumerate(["app-a", "app-b", "app-c", "app-d", "app-e"]):
        for k in range(1 + (ai % 3)):
            servers.append(Server(hostname=f"{app}-{k}", role="app", os="centos",
                                  app_ids=[app]))
    workloads = [
        Workload(app_id="app-data", name="data", tier="data", server_ids=["db-1", "cache-1"]),
        Workload(app_id="app-a", name="A", tier="backend", server_ids=["app-a-1"], depends_on=["app-data"]),
        Workload(app_id="app-b", name="B", tier="backend", server_ids=["app-b-1"], depends_on=["app-a"]),
        Workload(app_id="app-c", name="C", tier="backend", server_ids=["app-c-1", "app-c-2"], depends_on=["app-a"]),
        Workload(app_id="app-d", name="D", tier="frontend", server_ids=["app-d-1", "app-d-2", "app-d-3"], depends_on=["app-b"]),
        Workload(app_id="app-e", name="E", tier="frontend", server_ids=["app-e-1", "app-e-2"], depends_on=["app-c"]),
    ]
    return servers, workloads


# group_by="app" -> one wave per app + LZ + Data = 7 waves here
_OVER = {"notes": "over-split",
         "stages": [
             {"label": "LZ", "stage": "1_landing_zone", "roles": ["k8s", "monitoring"]},
             {"label": "Data", "stage": "2_data", "roles": ["db", "cache"], "depends_on": ["LZ"]},
             {"label": "Apps", "stage": "3_application", "group_by": "app",
              "use_app_deps": True, "depends_on": ["Data"]},
         ]}
# group_by="none" -> LZ + Data + Apps = 3 waves
_UNDER = {"notes": "capped",
          "stages": [
              {"label": "LZ", "stage": "1_landing_zone", "roles": ["k8s", "monitoring"]},
              {"label": "Data", "stage": "2_data", "roles": ["db", "cache"], "depends_on": ["LZ"]},
              {"label": "Apps", "stage": "3_application", "group_by": "none",
              "max_per_wave": 0, "depends_on": ["Data"]},
          ]}


def _planner():
    return get_planner(Settings(llm_enabled=True))


# ---------------------------------------------------------------------------
# cap parsing
# ---------------------------------------------------------------------------
def test_parse_max_waves_variants():
    assert _parse_max_waves("最多10个wave") == 10
    assert _parse_max_waves("最高分10个wave") == 10   # the user's actual typo
    assert _parse_max_waves("不超过8个wave") == 8
    assert _parse_max_waves("at most 5 waves") == 5
    assert _parse_max_waves("max 12 waves") == 12
    assert _parse_max_waves("prod DBs first, then apps") is None
    assert _parse_max_waves("") is None


# ---------------------------------------------------------------------------
# revision loop
# ---------------------------------------------------------------------------
def test_revision_loop_converges():
    servers, wls = _estate()
    calls = []

    def fake_chat(messages, stream=False, timeout=None):
        calls.append(messages)
        return json.dumps(_OVER if len(calls) == 1 else _UNDER, ensure_ascii=False)

    p = _planner()
    with patch.object(p.llm, "chat", side_effect=fake_chat):
        res = p.propose("最多 3 个 wave", servers, wls, matches=None)
    assert res["max_waves"] == 3
    assert res["waves_count"] == 3                  # converged to exactly the cap
    # propose returns the authoritative final waves (callers don't re-instantiate)
    assert res.get("waves") is not None and len(res["waves"]) == 3
    assert res["engine_capped"] is False             # LLM converged, no force-merge
    assert res["errors"] == [] and res["warnings"] == []
    assert len(res["revisions"]) == 1
    rev = res["revisions"][0]
    assert rev["waves_before"] == 7                 # 5 apps + LZ + Data
    assert rev["waves_after"] == 3
    assert rev["accepted"] is True
    assert len(calls) == 2                           # 1 propose + 1 revise
    assert not res["errors"]


def test_revision_loop_already_under_cap_skips():
    servers, wls = _estate()

    def fake_chat(messages, stream=False, timeout=None):
        return json.dumps(_UNDER, ensure_ascii=False)

    p = _planner()
    with patch.object(p.llm, "chat", side_effect=fake_chat):
        res = p.propose("最多 5 个 wave", servers, wls, matches=None)
    assert res["max_waves"] == 5
    assert res["waves_count"] == 3                  # already under cap
    assert res["revisions"] == []                   # no revision needed
    assert not res["errors"]


def test_revision_loop_never_converges_force_merged_to_cap():
    """Even when the LLM never produces a within-cap policy, the deterministic
    cap_waves backstop guarantees the final wave count <= cap."""
    servers, wls = _estate()
    calls = []

    def fake_chat(messages, stream=False, timeout=None):
        calls.append(messages)
        return json.dumps(_OVER, ensure_ascii=False)   # always over-split (7)

    p = _planner()
    with patch.object(p.llm, "chat", side_effect=fake_chat):
        res = p.propose("最多 2 个 wave", servers, wls, matches=None)
    assert res["max_waves"] == 2
    assert res["waves_count"] <= 2                       # HARD guarantee holds
    assert res["engine_capped"] is True                  # engine had to force-merge
    # force-merge is a warning (the plan is valid), not an error
    assert any("force-merged" in w for w in res["warnings"])
    assert res["errors"] == []
    assert len(res["revisions"]) == MAX_REVISIONS
    assert len(calls) == 1 + MAX_REVISIONS
    # the authoritative waves are returned (no re-instantiation needed)
    assert res.get("waves") is not None and len(res["waves"]) == res["waves_count"]


# ---------------------------------------------------------------------------
# deterministic cap_waves (the hard guarantee)
# ---------------------------------------------------------------------------
def _mk_waves(n, stage="3_application"):
    """n independent waves (no deps) — all incomparable, safe to merge."""
    return [Wave(name=f"W{i}", stage=stage, server_ids=[f"s{i}"]) for i in range(n)]


def test_cap_waves_under_cap_is_noop():
    ws = _mk_waves(3)
    out, notes = cap_waves(ws, 5)
    assert out is ws and notes == []


def test_cap_waves_merges_to_exactly_cap():
    ws = _mk_waves(10)
    out, notes = cap_waves(ws, 3)
    assert len(out) == 3                                 # exactly the cap
    # all original servers preserved, no dups
    sids = [s for w in out for s in w.server_ids]
    assert sorted(sids) == [f"s{i}" for i in range(10)]
    assert len(sids) == len(set(sids)) == 10
    # acyclic + all depends_on resolve
    val = validate_plan(out, [Server(id=f"s{i}", hostname=f"h{i}") for i in range(10)])
    assert val["ok"]


def test_cap_waves_respects_dependencies_no_cycle():
    """A→B→C chain (comparable waves). cap_waves must collapse to <= cap
    without creating a cycle (falls through to level collapse)."""
    a, b, c = Wave(name="A", server_ids=["s1"]), Wave(name="B", server_ids=["s2"]), \
        Wave(name="C", server_ids=["s3"])
    b.depends_on = [a.id]
    c.depends_on = [b.id]
    out, notes = cap_waves([a, b, c], cap=1)
    assert len(out) == 1
    assert out[0].depends_on == []                       # self-refs stripped
    val = validate_plan(out, [Server(id="s1"), Server(id="s2"), Server(id="s3")])
    assert val["ok"]


def test_cap_waves_cap_one_single_wave_fallback():
    ws = _mk_waves(50)
    out, notes = cap_waves(ws, 1)
    assert len(out) == 1
    assert len(out[0].server_ids) == 50


def test_build_from_policy_enforces_max_waves_hard():
    """End-to-end: a policy that would explode to many waves is capped by
    build_from_policy(max_waves=...)."""
    servers, wls = _estate()
    pol = WavePolicy.from_dict(_OVER)
    rep = build_from_policy(pol, servers, wls, matches=None, max_waves=3)
    assert len(rep.waves) <= 3                           # the hard guarantee
    # every server still assigned exactly once
    sids = [s for w in rep.waves for s in w.server_ids]
    assert len(sids) == len(set(sids))
    val = validate_plan(rep.waves, servers)
    assert val["ok"]


def test_no_cap_skips_revision():
    servers, wls = _estate()
    calls = []

    def fake_chat(messages, stream=False, timeout=None):
        calls.append(messages)
        return json.dumps(_OVER, ensure_ascii=False)

    p = _planner()
    with patch.object(p.llm, "chat", side_effect=fake_chat):
        res = p.propose("prod DBs first, then apps grouped by app", servers, wls)
    assert res["max_waves"] is None
    assert res["revisions"] == []
    assert res["waves_count"] == 7                   # over-split, but no cap -> fine
    assert not res["errors"]
    assert len(calls) == 1


def test_explicit_max_waves_overrides_demand_parse():
    servers, wls = _estate()
    captured = {}

    def fake_chat(messages, stream=False, timeout=None):
        # the propose (first) call embeds the demand; check it carries the cap phrasing
        if "最多" in messages[1]["content"]:
            captured["demand_present"] = True
        return json.dumps(_UNDER, ensure_ascii=False)

    p = _planner()
    # explicit 4 wins over a (larger) cap parsed from the demand
    with patch.object(p.llm, "chat", side_effect=fake_chat):
        res = p.propose("最多 10 个 wave", servers, wls, matches=None, max_waves=4)
    assert res["max_waves"] == 4                      # explicit arg wins
    assert res["waves_count"] == 3


def test_llm_disabled_returns_no_policy():
    servers, wls = _estate()
    p = get_planner(Settings(llm_enabled=False))
    res = p.propose("最多 3 个 wave", servers, wls)
    assert res["policy"] is None
    assert res["revisions"] == []
    assert res["waves_count"] is None
    assert res["max_waves"] == 3