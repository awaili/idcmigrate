"""F8 (Phase B) — LLM-driven Landing Zone design interview + design-aware
classification.

Phase A shipped the deterministic foundation (the ``lz_designs`` table, the
``LZDesign`` model, ``validate_lz_design``, active-design resolution). Phase B
adds the LLM interview that PRODUCES a design: ``LLMClient.design_lz`` proposes
a blueprint dict, the deterministic ``validate_lz_design`` loops it until
sound, and the design carries a ``requirements.classifier`` so a custom-named
design (hub/spoke/pci/...) actually classifies estate servers into its tiers
(closing the Phase A boundary where the built-in classifier never produced a
custom design's archetype names → workload_count 0).

The LLM path is mocked (``patch.object(c, "chat", ...)``) — same pattern as
``test_seven_r`` / ``test_planner_revision``. No real gateway is hit.
"""
import json
from unittest.mock import patch

import pytest

from idc.config import Settings
from idc.llm.client import LLMClient, _lz_estate_context
from idc.core.lz import (
    LZ_CORP, LZ_DMZ, LZ_ONLINE, LZ_PENDING, archetype_for, archetypes_for_servers,
    wave_archetypes, _classifier_from_design, active_classifier,
)
from idc.core.models import LZDesign, Server


# ---------------------------------------------------------------------------
# canned LLM design payloads (valid + invalid-so-it-loops)
# ---------------------------------------------------------------------------
def _valid_design(name="hub-spoke", onprem="172.16.0.0/12", pci=False):
    archs = {
        "hub": {
            "archetype": "hub", "summary": "hub",
            "vpc": {"name": "vpc-hub", "cidr": "10.10.0.0/16", "description": "hub"},
            "peering": {"to_spoke": True},
            "internet_egress": {"nat": True, "public_clb": False},
            "security_groups": ["sg-hub"], "cam_roles": ["r-hub"],
            "tag_policy": {"env": "required"},
            "policy_as_code": ["require env tag"]},
        "spoke": {
            "archetype": "spoke", "summary": "spoke",
            "vpc": {"name": "vpc-spoke", "cidr": "10.20.0.0/16", "description": "spoke"},
            "peering": {"to_hub": True},
            "internet_egress": {"nat": True, "public_clb": True, "cdn": True, "waf": True},
            "security_groups": ["sg-spoke"], "cam_roles": ["r-spoke"],
            "tag_policy": {"env": "required"},
            "policy_as_code": ["public clb behind waf"]},
        "isolated": {
            "archetype": "isolated", "summary": "isolated regulated",
            "vpc": {"name": "vpc-iso", "cidr": "10.30.0.0/16", "description": "iso"},
            "peering": {},
            "internet_egress": {"nat": False, "public_clb": False},
            "security_groups": ["sg-iso"], "cam_roles": ["r-iso"],
            "tag_policy": {"env": "required", "regulated": "required"},
            "policy_as_code": ["no internet ingress or egress"]},
    }
    if pci:
        archs["pci"] = {
            "archetype": "pci", "summary": "pci isolated",
            "vpc": {"name": "vpc-pci", "cidr": "10.40.0.0/16", "description": "pci"},
            "peering": {},
            "internet_egress": {"nat": False, "public_clb": False},
            "security_groups": ["sg-pci"], "cam_roles": ["r-pci"],
            "tag_policy": {"env": "required", "pci": "required"},
            "policy_as_code": ["no internet", "pci scope enforced"]}
    return {
        "name": name, "onprem_cidrs": [onprem],
        "summary": "hub-and-spoke: corp hub + online spoke + isolated dmz",
        "requirements": {"notes": "operator asked for hub-and-spoke",
                          "classifier": {"tag_map": {"idc:dmz": "isolated",
                                                     "idc:pci": "pci"} if pci
                                                    else {"idc:dmz": "isolated"},
                                         "role_map": {"web": "spoke", "frontend": "spoke",
                                                      "db": "hub"},
                                         "default": "hub"}},
        "archetypes": archs,
    }


def _asymmetric_design():
    """Same as the valid one but spoke has NO to_hub → asymmetric peering, so the
    validator rejects it and design_lz must loop."""
    d = _valid_design()
    d["archetypes"]["spoke"]["peering"] = {}      # drop to_hub
    return d


def _srv(role="", tags=None, sid="s", app_ids=None):
    return Server(id=sid, hostname=sid, role=role, tags=tags or [],
                  app_ids=app_ids or [])


# ---------------------------------------------------------------------------
# design-aware classification (pure, no DB, no LLM)
# ---------------------------------------------------------------------------
def test_archetype_for_without_classifier_is_builtin():
    """No classifier = the built-in tag/role logic (back-compat): internal roles
    -> corp, web -> online, dmz tag -> dmz; a signal-less host -> pending."""
    assert archetype_for(_srv(role="web")) == LZ_ONLINE
    assert archetype_for(_srv(role="db")) == LZ_CORP
    assert archetype_for(_srv(role="db", tags=["idc:dmz"])) == LZ_DMZ
    assert archetype_for(_srv()) == LZ_PENDING


def test_archetype_for_classifier_tag_map_wins():
    clf = {"tag_map": {"idc:dmz": "isolated"}, "role_map": {"web": "spoke"}}
    # a db host with the dmz tag -> isolated (tag beats role under a classifier)
    assert archetype_for(_srv(role="db", tags=["idc:dmz"]), classifier=clf) == "isolated"
    # a web host -> spoke (role_map)
    assert archetype_for(_srv(role="web"), classifier=clf) == "spoke"
    # an unknown role, no tag -> undetermined -> pending (no catch-all default)
    assert archetype_for(_srv(role="cache"), classifier=clf) == LZ_PENDING


def test_archetype_for_classifier_ignores_builtin_hints():
    """Under a custom design the built-in corp/online/dmz fallbacks are NOT
    consulted — the design owns the mapping (its archetype names are arbitrary)."""
    clf = {"tag_map": {}, "role_map": {"db": "hub"}}
    # a web host that the built-in classifier would send to 'online' is NOT mapped
    # by this design (web not in role_map) -> undetermined -> pending
    assert archetype_for(_srv(role="web"), classifier=clf) == LZ_PENDING


def test_archetype_for_classifier_app_map_routes_by_app():
    """app_map routes a server to an account by which app it hosts — the
    multi-workload-account signal. It beats role_map (an app assigned to the
    'online' account goes there even if its role is db) but loses to an explicit
    operator tag."""
    clf = {"tag_map": {}, "app_map": {"APP_0001": "data", "APP_0073": "online"},
           "role_map": {"db": "data", "web": "online"}}
    # a db host hosting APP_0073 -> online (app beats role)
    assert archetype_for(_srv(role="db", app_ids=["APP_0073"]), classifier=clf) == "online"
    # a web host hosting APP_0001 -> data (app beats role)
    assert archetype_for(_srv(role="web", app_ids=["APP_0001"]), classifier=clf) == "data"
    # a host whose app is not in app_map falls through to role_map
    assert archetype_for(_srv(role="db", app_ids=["APP_X"]), classifier=clf) == "data"
    # a host with no app + a role not in role_map -> undetermined -> pending
    assert archetype_for(_srv(role="cache"), classifier=clf) == LZ_PENDING


def test_archetype_for_classifier_tag_beats_app_map():
    """An explicit operator tag (tag_map) wins over app_map — the operator can
    always override an app-based account assignment for a single host."""
    clf = {"tag_map": {"idc:dmz": "isolated"},
           "app_map": {"APP_0001": "data"},
           "role_map": {}, "default": "corp"}
    assert archetype_for(_srv(role="db", tags=["idc:dmz"], app_ids=["APP_0001"]),
                         classifier=clf) == "isolated"


def test_archetype_for_classifier_app_map_first_app_wins_deterministically():
    """A server hosting several app_map apps resolves to one account by the first
    mapped app in its (stable) app_ids list — deterministic across calls."""
    clf = {"tag_map": {}, "app_map": {"APP_A": "data", "APP_B": "online"},
           "role_map": {}, "default": "corp"}
    s = _srv(role="app", app_ids=["APP_B", "APP_A"])   # APP_B listed first
    assert archetype_for(s, classifier=clf) == "online"
    # same result every call
    assert len({archetype_for(s, classifier=clf) for _ in range(50)}) == 1


def test_classifier_from_design_filters_app_map_to_known_archetypes():
    """app_map entries pointing at archetypes the design doesn't define are
    dropped (a stale assignment after an archetype rename is harmless), and the
    app_map is returned alongside tag_map/role_map (no default — undetermined
    hosts are pending)."""
    d = LZDesign(design_id="d", name="d",
                 archetypes={"data": {}, "online": {}, "corp": {}},
                 requirements={"classifier": {
                     "tag_map": {},
                     "app_map": {"APP_0001": "data", "APP_DEAD": "ghost"},  # ghost gone
                     "role_map": {"web": "online"},
                     "default": "corp"}})
    clf = _classifier_from_design(d)
    assert clf["app_map"] == {"APP_0001": "data"}
    # and it routes a real server
    assert archetype_for(_srv(role="db", app_ids=["APP_0001"]), classifier=clf) == "data"


def test_archetype_for_classifier_tag_map_is_deterministic_on_conflict():
    """A server carrying several tag_map tags resolves to ONE archetype
    deterministically (sorted tag order), not hash-order-dependent. Two calls
    on the same server always agree; the lexicographically-first mapped tag wins."""
    clf = {"tag_map": {"idc:dmz": "isolated", "idc:online": "spoke"},
           "role_map": {}, "default": "hub"}
    s = _srv(role="app", tags=["idc:online", "idc:dmz"])   # both tags map
    # "idc:dmz" < "idc:online" -> isolated wins, reproducibly
    results = {archetype_for(s, classifier=clf) for _ in range(50)}
    assert results == {"isolated"}     # never "spoke", never varies


def test_archetypes_for_servers_threads_classifier():
    clf = {"tag_map": {}, "role_map": {"web": "spoke", "db": "hub"}}
    ss = [_srv(role="web", sid="a"), _srv(role="db", sid="b")]
    out = archetypes_for_servers(ss, classifier=clf)
    assert out == {"a": "spoke", "b": "hub"}
    # without a classifier the built-in logic runs (db -> corp, the internal hub)
    assert archetypes_for_servers(ss)["b"] == LZ_CORP


def test_wave_archetypes_uses_design_names_with_classifier():
    """A custom-named design shows a real workload mix instead of all-zero
    counts (the built-in classifier can't produce hub/spoke)."""
    clf = {"tag_map": {}, "role_map": {"web": "spoke", "db": "hub"}}
    ss = [_srv(role="web", sid="a"), _srv(role="db", sid="b"), _srv(role="cache", sid="c")]
    mix = wave_archetypes(["a", "b", "c"], ss, classifier=clf,
                          archetype_names=["hub", "spoke"])
    assert mix == {"hub": 1, "spoke": 1, LZ_PENDING: 1}   # cache undetermined -> pending


def test_wave_archetypes_no_classifier_back_compat():
    """No classifier + no archetype_names = the original built-in mix keys."""
    ss = [_srv(role="web", sid="a")]
    mix = wave_archetypes(["a"], ss)
    assert mix.get(LZ_ONLINE) == 1


def test_classifier_from_design_filters_unknown_targets():
    """A classifier that points at archetypes the design no longer defines is
    filtered out (a stale map after an archetype rename must not route servers
    into a non-existent tier). The ``default`` catch-all is dropped on read —
    undetermined hosts are pending, not absorbed into a default account."""
    d = LZDesign(design_id="d", name="d",
                 archetypes={"hub": {}, "spoke": {}},
                 requirements={"classifier": {
                     "tag_map": {"idc:dmz": "gone"},   # 'gone' not in archetypes
                     "role_map": {"web": "spoke"},
                     "default": "hub"}})
    clf = _classifier_from_design(d)
    assert clf == {"tag_map": {}, "app_map": {}, "role_map": {"web": "spoke"}}


def test_classifier_from_design_none_when_everything_filtered():
    d = LZDesign(design_id="d", name="d", archetypes={"hub": {}},
                 requirements={"classifier": {"tag_map": {"x": "nope"},
                                              "role_map": {"y": "also-nope"},
                                              "default": "missing"}})
    assert _classifier_from_design(d) is None


def test_classifier_from_design_none_for_builtin():
    """The built-in default design has no classifier -> None -> built-in path."""
    from idc.core.lz import default_lz_design
    assert _classifier_from_design(default_lz_design()) is None


# ---------------------------------------------------------------------------
# estate-context builder
# ---------------------------------------------------------------------------
def test_lz_estate_context_has_distributions_and_hints():
    ss = [_srv(role="web", sid="a"), _srv(role="db", sid="b"),
          Server(id="c", hostname="c", role="web", env="prod",
                 business_criticality="high", tags=["idc:dmz"], subnet="10.0.4.0/24")]
    ctx = _lz_estate_context(ss, onprem_cidrs=["172.16.0.0/12"])
    obj = json.loads(ctx.split("JSON):\n", 1)[1])
    assert obj["total_servers"] == 3
    assert obj["by_role"] == {"web": 2, "db": 1}
    assert obj["by_env"] == {"prod": 1}
    assert obj["by_criticality"] == {"high": 1}
    assert "idc:dmz" in obj["regulation_tag_hints"]
    assert obj["onprem_subnet_hints"] == ["10.0.4.0/24"]
    assert obj["operator_supplied_onprem_cidrs"] == ["172.16.0.0/12"]


# ---------------------------------------------------------------------------
# design_lz — the LLM interview (mocked)
# ---------------------------------------------------------------------------
def _client():
    return LLMClient(Settings(llm_enabled=True))


def test_design_lz_valid_first_try():
    c = _client()
    with patch.object(c, "chat", return_value=json.dumps(_valid_design())):
        r = c.design_lz("hub and spoke, regulated workloads isolated", [_srv(role="web")])
    assert r["ok"] is True
    assert r["error"] == ""
    assert r["design"].name == "hub-spoke"
    assert set(r["design"].archetypes) == {"hub", "spoke", "isolated"}
    assert r["validation"]["ok"] is True
    assert len(r["rounds"]) == 1 and r["rounds"][0]["ok"] is True
    # the conversation carries the opening user turn + the assistant reply
    assert r["design"].conversation[0]["role"] == "user"
    assert r["design"].conversation[-1]["role"] == "assistant"
    # the LLM's top-level ``summary`` (the AI description surfaced in the Saved
    # designs table) is stamped on the design
    assert r["design"].summary == "hub-and-spoke: corp hub + online spoke + isolated dmz"


def test_design_lz_summary_falls_back_to_requirements_notes():
    """When the LLM omits a top-level ``summary`` (older / terser output), the
    AI description falls back to ``requirements.notes`` (also AI-authored) so the
    Saved designs column is never empty."""
    c = _client()
    d = _valid_design()
    d.pop("summary", None)
    with patch.object(c, "chat", return_value=json.dumps(d)):
        r = c.design_lz("hub and spoke", [_srv(role="web")])
    assert r["ok"] is True
    # falls back to requirements.notes ("operator asked for hub-and-spoke")
    assert r["design"].summary == "operator asked for hub-and-spoke"
    """First reply has asymmetric peering (validator rejects); the second fixes
    it. design_lz feeds the deterministic errors back and loops."""
    calls = []

    def fake_chat(messages, stream=False, timeout=None):
        calls.append(messages)
        return json.dumps(_asymmetric_design() if len(calls) == 1 else _valid_design())

    c = _client()
    with patch.object(c, "chat", side_effect=fake_chat):
        r = c.design_lz("hub and spoke", [_srv(role="web")])
    assert r["ok"] is True
    assert len(r["rounds"]) == 2
    assert r["rounds"][0]["ok"] is False
    assert any("asymmetric peering" in e for e in r["rounds"][0]["errors"])
    assert r["rounds"][1]["ok"] is True
    # the second LLM call saw the validator's error feedback as a user turn
    feedback = " ".join(m["content"] for m in calls[1] if m["role"] == "user")
    assert "asymmetric peering" in feedback


def test_design_lz_iterate_continues_conversation():
    """A follow-up turn preserves the prior conversation + extends it, and does
    NOT re-inject the estate context (the LLM remembers the archetypes)."""
    prior = [{"role": "user", "content": "hub and spoke"},
             {"role": "assistant", "content": json.dumps(_valid_design())}]
    captured = []

    def fake_chat(messages, stream=False, timeout=None):
        captured.append(messages)
        return json.dumps(_valid_design(name="hub-spoke-pci", pci=True))

    c = _client()
    with patch.object(c, "chat", side_effect=fake_chat):
        r = c.design_lz("add a pci tier", [_srv(role="web")],
                        conversation=prior)
    assert r["ok"] is True
    assert "pci" in r["design"].archetypes
    conv = r["design"].conversation
    assert conv[:2] == prior                       # prior turns preserved
    assert len(conv) == len(prior) + 2             # + new user demand + assistant
    assert conv[-2]["role"] == "user" and conv[-2]["content"] == "add a pci tier"
    # the new user turn was the bare demand (no estate-context preamble on iterate)
    assert "Estate summary" not in captured[0][-1]["content"]


def test_design_lz_first_turn_includes_estate_context():
    """The opening turn frames the demand with the estate summary so the LLM
    grounds the proposal in the real estate."""
    captured = []

    def fake_chat(messages, stream=False, timeout=None):
        captured.append(messages)
        return json.dumps(_valid_design())

    c = _client()
    with patch.object(c, "chat", side_effect=fake_chat):
        c.design_lz("hub and spoke", [_srv(role="web")])
    user0 = next(m for m in captured[0] if m["role"] == "user")
    assert "Estate summary" in user0["content"]
    assert "hub and spoke" in user0["content"]


def test_design_lz_disabled_returns_builtin_default():
    c = LLMClient(Settings(llm_enabled=False))
    r = c.design_lz("hub and spoke", [_srv(role="web")])
    assert r["ok"] is False
    assert "disabled" in r["error"]
    # graceful degradation: the built-in default (strict superset) so callers
    # keep working — never None, never a crash.
    from idc.core.lz import default_lz_design
    assert set(r["design"].archetypes) == set(default_lz_design().archetypes)


def test_design_lz_unparseable_returns_error():
    c = _client()
    with patch.object(c, "chat", return_value="sorry, i can't do that"):
        r = c.design_lz("hub and spoke", [_srv(role="web")], max_rounds=2)
    assert r["ok"] is False
    assert r["rounds"]
    assert r["rounds"][0]["errors"] == ["output was not valid JSON"]


def test_design_lz_never_valid_returns_last_attempt_with_conversation():
    """When the LLM keeps emitting invalid designs, the last attempt (with its
    conversation + the validator errors) is returned so the UI can show what
    was tried — not silently the built-in default."""
    c = _client()
    with patch.object(c, "chat", return_value=json.dumps(_asymmetric_design())):
        r = c.design_lz("hub and spoke", [_srv(role="web")], max_rounds=2)
    assert r["ok"] is False
    assert r["design"] is not None
    assert r["design"].archetypes               # the attempted (invalid) archetypes
    assert r["design"].conversation            # the conversation is preserved
    assert r["validation"]["ok"] is False


# ---------------------------------------------------------------------------
# backend integration: a custom-named design WITH a classifier drives
# /api/lz/archetypes workload counts (closes the Phase A boundary)
# ---------------------------------------------------------------------------
pytest.importorskip("idc.backend.app")
from fastapi.testclient import TestClient  # noqa: E402
import idc.backend.app as m  # noqa: E402
from idc.core.db import open_store  # noqa: E402
from idc.config import get_settings  # noqa: E402
from idc.core.models import _new_id, Server  # noqa: E402

client = TestClient(m.app)


def _cleanup(name, sids):
    st = open_store(get_settings().db_url)
    with st.tx() as cur:
        cur.execute(st._x("UPDATE lz_designs SET is_active=0"))
        cur.execute(st._x("DELETE FROM lz_designs WHERE name=?"), (name,))
        for sid in sids:
            cur.execute(st._x("DELETE FROM servers WHERE id=?"), (sid,))
    st.close()


def test_active_classifier_drives_archetypes_workload_counts():
    """A custom hub/spoke design with a classifier classifies estate servers into
    hub/spoke (so the counts are non-zero), where the Phase A boundary left the
    built-in classifier unable to produce those names."""
    name = "interview-hubspoke"
    w_sid, d_sid = _new_id("srv"), _new_id("srv")
    try:
        st = open_store(get_settings().db_url)
        st.upsert_server(Server(id=w_sid, hostname="web-1", role="web"))
        st.upsert_server(Server(id=d_sid, hostname="db-1", role="db"))
        st.close()
        body = {"name": name, "onprem_cidrs": ["172.16.0.0/12"],
                "requirements": _valid_design()["requirements"],
                "archetypes": _valid_design()["archetypes"], "updated_by": "test"}
        cr = client.post("/api/lz/designs", json=body)
        assert cr.status_code == 200, cr.text
        did = next(d["design_id"] for d in client.get("/api/lz/designs").json()["designs"]
                   if d["name"] == name)
        ar = client.post(f"/api/lz/designs/{did}/activate", json={"by": "test"})
        assert ar.status_code == 200, ar.text
        # /api/lz/archetypes now classifies web->spoke, db->hub via the design.
        # Assert via per_server for OUR seeded servers (the shared test DB holds
        # other servers too, so exact counts aren't stable — see the
        # regression-tests-share-db memory).
        arch = client.get("/api/lz/archetypes").json()
        assert arch["design"]["is_builtin"] is False
        assert arch["design"]["has_classifier"] is True
        assert set(arch["archetypes"]) == {"hub", "spoke", "isolated"}
        by_id = {r["server_id"]: r["archetype"] for r in arch["per_server"]}
        assert by_id[w_sid] == "spoke"      # role web -> spoke (role_map)
        assert by_id[d_sid] == "hub"        # role db  -> hub  (role_map)
        assert arch["counts"].get("spoke", 0) >= 1
        assert arch["counts"].get("hub", 0) >= 1
        # active_classifier resolves the same map the engine uses
        st = open_store(get_settings().db_url)
        clf = active_classifier(st)
        st.close()
        assert clf["role_map"]["web"] == "spoke"
    finally:
        _cleanup(name, [w_sid, d_sid])