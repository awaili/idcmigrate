"""Tests for the grounded estate-query engine + ask_grounded two-step."""
import json
from unittest.mock import patch

import pytest

from idc.config import Settings
from idc.core.models import Server, Utilization, Wave, Workload, AppStrategy
from idc.core.query import INTENTS, estate_query
from idc.llm.client import LLMClient


def _estate():
    """3 apps A->B->C (A deps B, B deps C). whatif_delayed C blocks B,A."""
    sA = Server(hostname="a", role="app", app_ids=["A"], env="prod",
                business_criticality="high", utilization=Utilization(cpu_p95=90))
    sB = Server(hostname="b", role="app", app_ids=["B"], env="dev",
                utilization=Utilization())
    sC = Server(hostname="c", role="web", app_ids=["C"], env="prod",
                business_criticality="high", utilization=Utilization())
    wls = [Workload(app_id="A", name="A", server_ids=[sA.id], depends_on=["B"]),
           Workload(app_id="B", name="B", server_ids=[sB.id], depends_on=["C"]),
           Workload(app_id="C", name="C", server_ids=[sC.id])]
    waves = [Wave(name="WA", stage="3_application", server_ids=[sA.id]),
            Wave(name="WB", stage="3_application", server_ids=[sB.id], depends_on=["wC"]),
            Wave(name="WC Cutover", stage="4_cutover", server_ids=[sC.id])]
    return [sA, sB, sC], waves, wls


CTX_KW = dict(matches=[], profiles=[], strategies={})


# ---------------------------------------------------------------------------
# deterministic engine
# ---------------------------------------------------------------------------
def test_intents_canonical():
    assert "whatif_delayed" in INTENTS and "unknown" in INTENTS


def test_whatif_delayed_reverse_reachable():
    servers, waves, wls = _estate()
    r = estate_query("whatif_delayed", {"app_id": "C"},
                     servers=servers, waves=waves, workloads=wls, **CTX_KW)
    assert r["blocked_apps"] == ["A", "B"] and r["blocked_app_count"] == 2
    assert "WA" in " ".join(r["blocked_waves"])


def test_deps_of_and_dependents_of():
    servers, waves, wls = _estate()
    assert estate_query("deps_of", {"app_id": "A"},
                        servers=servers, waves=waves, workloads=wls, **CTX_KW)["depends_on"] == ["B"]
    assert estate_query("dependents_of", {"app_id": "C"},
                        servers=servers, waves=waves, workloads=wls, **CTX_KW)["dependents"] == ["B"]


def test_wave_for_app():
    servers, waves, wls = _estate()
    r = estate_query("wave_for_app", {"app_id": "C"},
                     servers=servers, waves=waves, workloads=wls, **CTX_KW)
    assert len(r["waves"]) == 1 and r["waves"][0]["name"] == "WC Cutover"


def test_riskiest_waves_orders_by_score():
    servers, waves, wls = _estate()
    r = estate_query("riskiest_waves", {"n": 2},
                     servers=servers, waves=waves, workloads=wls, **CTX_KW)
    assert r["top_n"] == 2 and len(r["waves"]) == 2
    # ordered by score desc; cutover (high-crit) scores highest here
    scores = [w["score"] for w in r["waves"]]
    assert scores == sorted(scores, reverse=True)
    assert r["waves"][0]["name"] == "WC Cutover"


def test_aggregate_cutover_by_env():
    servers, waves, wls = _estate()
    r = estate_query("aggregate", {"scope": "cutover", "metric": "env"},
                     servers=servers, waves=waves, workloads=wls, **CTX_KW)
    assert r["total"] == 1 and r["distribution"].get("prod") == 1


def test_aggregate_all_by_criticality():
    servers, waves, wls = _estate()
    r = estate_query("aggregate", {"metric": "criticality"},
                     servers=servers, waves=waves, workloads=wls, **CTX_KW)
    assert r["total"] == 3 and r["distribution"]["high"] == 2


def test_retain_retire_from_strategies():
    servers, waves, wls = _estate()
    strats = {"A": AppStrategy(app_id="A", strategy="retain", rationale="regulated")}
    r = estate_query("retain_retire", {},
                     servers=servers, waves=waves, workloads=wls, **{**CTX_KW, "strategies": strats})
    assert r["count"] == 1 and r["apps"][0]["app_id"] == "A"


def test_search_finds_by_hostname():
    servers, waves, wls = _estate()
    r = estate_query("search", {"q": "b"},
                     servers=servers, waves=waves, workloads=wls, **CTX_KW)
    assert any(h["hostname"] == "b" for h in r["matches"])


def test_search_oracle_database_finds_db_hosts():
    # Oracle DB hosts are tagged "oracle-db" (role "db"); the phrase
    # "oracle database" never appears verbatim, so the search must be
    # token-aware (database→db, split oracle-db) to find them.
    servers, _, _ = _estate()
    servers = servers + [
        Server(hostname="ora-prod-01", role="db", tags=["oracle-db"], env="prod"),
        Server(hostname="pg-repl-02", role="db", tags=["db:postgres"], env="prod"),
        Server(hostname="app-web-03", role="app", tags=["mw:tomcat"], env="dev"),
        # MySQL host running on Oracle Linux — "oracle" must NOT match via the
        # OS when the query is "oracle database" (the canonicalization to the
        # oracle-db tag excludes this false positive).
        Server(hostname="mysql-on-ol", role="db", tags=["db:mysql", "mysql"],
               os="oracle linux", env="prod"),
    ]
    r = estate_query("search", {"q": "oracle database"},
                     servers=servers, waves=[], workloads=[], **CTX_KW)
    hostnames = {h["hostname"] for h in r["matches"]}
    assert "ora-prod-01" in hostnames          # oracle DB host matches
    assert "pg-repl-02" not in hostnames        # postgres is not oracle
    assert "app-web-03" not in hostnames
    assert "mysql-on-ol" not in hostnames       # Oracle Linux OS is not Oracle DB
    assert r["count"] == 1 and r["shown"] == len(r["matches"])
    # single-token "oracle" also works (substring of oracle-db)
    r2 = estate_query("search", {"q": "oracle"},
                      servers=servers, waves=[], workloads=[], **CTX_KW)
    assert "ora-prod-01" in {h["hostname"] for h in r2["matches"]}
    # "database" alone folds to db -> matches all db hosts (incl. the OL one)
    r3 = estate_query("search", {"q": "database"},
                      servers=servers, waves=[], workloads=[], **CTX_KW)
    assert {"ora-prod-01", "pg-repl-02", "mysql-on-ol"} <= {h["hostname"] for h in r3["matches"]}


def test_search_caps_payload_but_reports_true_total():
    servers, _, _ = _estate()
    servers = servers + [
        Server(hostname=f"ora-{i:03d}", role="db", tags=["oracle-db"], env="prod")
        for i in range(60)]
    r = estate_query("search", {"q": "oracle"},
                     servers=servers, waves=[], workloads=[], **CTX_KW)
    assert r["count"] == 60          # true total
    assert r["shown"] == 30          # payload capped
    assert r["truncated"] is True
    assert len(r["matches"]) == 30


def test_unknown_intent_returns_hint():
    servers, waves, wls = _estate()
    r = estate_query("nonsense", {}, servers=servers, waves=waves, workloads=wls, **CTX_KW)
    assert r["intent"] == "unknown" and r["valid_intents"]


# ---------------------------------------------------------------------------
# ask_grounded two-step (mocked LLM)
# ---------------------------------------------------------------------------
def test_ask_grounded_classify_query_narrate():
    servers, waves, wls = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    calls = []

    def fake_chat(messages, stream=False, timeout=None):
        calls.append(messages)
        if len(calls) == 1:  # classify
            return json.dumps({"intent": "whatif_delayed", "params": {"app_id": "C"}})
        return "Delaying C blocks apps A and B (waves WA, WB)."
    with patch.object(c, "chat", side_effect=fake_chat):
        r = c.ask_grounded("what if C is delayed?", servers, waves, wls, [], [], {})
    assert r["ok"] and r["intent"] == "whatif_delayed"
    assert r["result"]["blocked_apps"] == ["A", "B"]   # deterministic, not LLM
    assert "A and B" in r["answer"]


def test_ask_grounded_bad_intent_falls_to_unknown():
    servers, waves, wls = _estate()
    c = LLMClient(Settings(llm_enabled=True))
    # classify returns a bogus intent -> normalized to unknown; narrate returns text
    def fake_chat(messages, stream=False, timeout=None):
        return json.dumps({"intent": "frobnicate", "params": {}})
    with patch.object(c, "chat", side_effect=fake_chat):
        r = c.ask_grounded("???", servers, waves, wls, [], [], {})
    assert r["intent"] == "unknown"   # normalized; engine returns a hint result


def test_ask_grounded_disabled_returns_fallback():
    servers, waves, wls = _estate()
    c = LLMClient(Settings(llm_enabled=False))
    r = c.ask_grounded("anything", servers, waves, wls, [], [], {})
    assert not r["ok"] and "unavailable" in r["answer"]